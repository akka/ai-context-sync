/**
 * Cloudflare Worker — Claude Context Distribution
 *
 * Responsibilities:
 *  - Cron trigger: fetch all .md files from the private GitHub repo, store in KV
 *  - HTTP GET /manifest.json  → returns JSON index of all context files + their content
 *  - HTTP GET /contexts/*     → returns a single raw context file
 *  - All HTTP endpoints require  Authorization: Bearer <CONTEXT_API_KEY>
 *
 * Required Worker secrets (set via wrangler secret put or dashboard):
 *   GITHUB_TOKEN      — bot PAT with contents:read on the repo
 *   CONTEXT_API_KEY   — shared key distributed to employees
 *
 * Required KV namespace binding (see wrangler.toml):
 *   CONTEXTS_KV
 */

// ── Config (edit these or move to env vars) ───────────────────────────────────
const GITHUB_REPO   = "akka/ai-context-sync";
const GITHUB_BRANCH = "main";
const CACHE_TTL_SEC = 90000; // KV edge TTL (~25 hours — longer than cron interval)

// Content lives in the .claude/ subtree of this repo, already structured to
// match what employees need under ~/.claude/. Strip the leading ".claude/" when
// storing so served paths are e.g. "contexts/index.md", "skills/ciso/SKILL.md".
const CONTENT_PREFIX  = ".claude/";
const INCLUDE_SUBDIRS = new Set(["contexts", "skills", "commands"]);

// ── KV key constants ──────────────────────────────────────────────────────────
const MANIFEST_KEY  = "__manifest__";
const SYNCED_AT_KEY = "__synced_at__";

// ── Auth ──────────────────────────────────────────────────────────────────────

function checkAuth(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const [scheme, token] = auth.split(" ");
  if (scheme !== "Bearer" || token !== env.CONTEXT_API_KEY) {
    return new Response(
      JSON.stringify({ error: "Unauthorized" }),
      { status: 401, headers: { "Content-Type": "application/json" } }
    );
  }
  return null; // auth ok
}

// ── GitHub helpers ────────────────────────────────────────────────────────────

async function ghFetch(path, token) {
  const resp = await fetch(`https://api.github.com${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github.v3+json",
      "User-Agent": "akka-claude-context-worker/1.0",
    },
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`GitHub API ${resp.status} for ${path}: ${body.slice(0, 200)}`);
  }
  return resp.json();
}

async function ghGraphQL(query, token) {
  const resp = await fetch("https://api.github.com/graphql", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "User-Agent": "akka-claude-context-worker/1.0",
    },
    body: JSON.stringify({ query }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`GitHub GraphQL ${resp.status}: ${body.slice(0, 200)}`);
  }
  const json = await resp.json();
  if (json.errors) {
    throw new Error(`GitHub GraphQL errors: ${JSON.stringify(json.errors)}`);
  }
  return json.data;
}

// ── Sync logic (runs on cron) ─────────────────────────────────────────────────

async function syncFromGitHub(env) {
  console.log(`Syncing from github.com/${GITHUB_REPO} (${GITHUB_BRANCH})…`);

  // Subrequest 1: REST tree — gets all file paths and SHAs in one call
  const tree = await ghFetch(
    `/repos/${GITHUB_REPO}/git/trees/${GITHUB_BRANCH}?recursive=1`,
    env.GITHUB_TOKEN
  );

  const mdBlobs = tree.tree.filter((item) => {
    if (item.type !== "blob" || !item.path.endsWith(".md")) return false;
    if (!item.path.startsWith(CONTENT_PREFIX)) return false;
    const sub = item.path.slice(CONTENT_PREFIX.length).split("/")[0];
    return INCLUDE_SUBDIRS.has(sub);
  });

  console.log(`Found ${mdBlobs.length} matching .md file(s)`);

  if (mdBlobs.length === 0) {
    const manifest = { files: {}, synced_at: new Date().toISOString() };
    await env.CONTEXTS_KV.put(MANIFEST_KEY, JSON.stringify(manifest), { expirationTtl: CACHE_TTL_SEC });
    await env.CONTEXTS_KV.put(SYNCED_AT_KEY, manifest.synced_at);
    return manifest;
  }

  // Subrequest 2: GraphQL with one alias per blob — fetches all file contents in one request
  const aliases = mdBlobs.map((b, i) =>
    `f${i}: object(oid: "${b.sha}") { ... on Blob { text } }`
  ).join("\n");

  const gqlData = await ghGraphQL(`
    {
      repository(owner: "${GITHUB_REPO.split("/")[0]}", name: "${GITHUB_REPO.split("/")[1]}") {
        ${aliases}
      }
    }
  `, env.GITHUB_TOKEN);

  const manifest = { files: {}, synced_at: new Date().toISOString() };

  // KV puts are binding calls, not fetch subrequests — they don't count toward the limit
  for (let i = 0; i < mdBlobs.length; i++) {
    const rawPath  = mdBlobs[i].path;
    const path     = rawPath.startsWith(CONTENT_PREFIX)
      ? rawPath.slice(CONTENT_PREFIX.length)
      : rawPath;
    const text = gqlData?.repository?.[`f${i}`]?.text ?? "";
    await env.CONTEXTS_KV.put(`file:${path}`, text, { expirationTtl: CACHE_TTL_SEC });
    manifest.files[path] = { path, size: text.length, content: text };
    console.log(`  stored ${path} (${text.length} bytes)`);
  }

  await env.CONTEXTS_KV.put(MANIFEST_KEY, JSON.stringify(manifest), {
    expirationTtl: CACHE_TTL_SEC,
  });
  await env.CONTEXTS_KV.put(SYNCED_AT_KEY, manifest.synced_at);

  console.log(`Sync complete. ${mdBlobs.length} files stored in KV.`);
  return manifest;
}

// ── HTTP request handler ──────────────────────────────────────────────────────

async function handleRequest(request, env) {
  const url   = new URL(request.url);
  const path  = url.pathname;

  // Health check endpoint — no auth required
  if (path === "/health") {
    const synced = await env.CONTEXTS_KV.get(SYNCED_AT_KEY);
    return new Response(
      JSON.stringify({ status: "ok", last_sync: synced ?? "never" }),
      { headers: { "Content-Type": "application/json" } }
    );
  }

  // All other endpoints require auth
  const authError = checkAuth(request, env);
  if (authError) return authError;

  // GET /manifest.json — full manifest with all file content
  if (path === "/manifest.json" && request.method === "GET") {
    let manifest = await env.CONTEXTS_KV.get(MANIFEST_KEY);
    if (!manifest) {
      // Cold start — sync now (first deploy or KV expired)
      const data = await syncFromGitHub(env);
      manifest = JSON.stringify(data);
    }
    return new Response(manifest, {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store", // don't cache on the client — KV is the cache
      },
    });
  }

  // GET /contexts/<path> — single file
  if (path.startsWith("/contexts/") && request.method === "GET") {
    const filePath = path.replace("/contexts/", "");
    const content  = await env.CONTEXTS_KV.get(`file:${filePath}`);
    if (!content) {
      return new Response(JSON.stringify({ error: "Not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(content, {
      headers: { "Content-Type": "text/markdown; charset=utf-8" },
    });
  }

  // POST /sync — manual trigger for admins (same API key)
  if (path === "/sync" && request.method === "POST") {
    try {
      const manifest = await syncFromGitHub(env);
      return new Response(
        JSON.stringify({
          ok: true,
          files: Object.keys(manifest.files).length,
          synced_at: manifest.synced_at,
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    } catch (err) {
      return new Response(
        JSON.stringify({ ok: false, error: err.message }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }
  }

  return new Response(JSON.stringify({ error: "Not found" }), {
    status: 404,
    headers: { "Content-Type": "application/json" },
  });
}

// ── Exports ───────────────────────────────────────────────────────────────────

export default {
  // HTTP handler
  async fetch(request, env, ctx) {
    try {
      return await handleRequest(request, env);
    } catch (err) {
      console.error("Unhandled error:", err);
      return new Response(
        JSON.stringify({ error: "Internal server error" }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }
  },

  // Cron trigger — runs daily (configured in wrangler.toml)
  async scheduled(event, env, ctx) {
    ctx.waitUntil(syncFromGitHub(env));
  },
};
