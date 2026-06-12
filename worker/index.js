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
const GITHUB_REPO   = "akka/org-ai-contexts";
const GITHUB_BRANCH = "main";
const CACHE_TTL_SEC = 90000; // KV edge TTL (~25 hours — longer than cron interval)

// Paths to include from the repo (by top-level directory or exact name).
// Everything else is ignored — repo metadata, generated bundles, etc.
const INCLUDE_TOPS  = new Set(["context", "skills", "prompts"]);
const INCLUDE_EXACT = new Set(["index.md"]);

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

async function fetchRaw(url, token) {
  const resp = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": "akka-claude-context-worker/1.0",
    },
  });
  if (!resp.ok) throw new Error(`Failed to fetch ${url}: ${resp.status}`);
  return resp.text();
}

// ── Sync logic (runs on cron) ─────────────────────────────────────────────────

async function syncFromGitHub(env) {
  console.log(`Syncing from github.com/${GITHUB_REPO} (${GITHUB_BRANCH})…`);

  const tree = await ghFetch(
    `/repos/${GITHUB_REPO}/git/trees/${GITHUB_BRANCH}?recursive=1`,
    env.GITHUB_TOKEN
  );

  const mdBlobs = tree.tree.filter((item) => {
    if (item.type !== "blob" || !item.path.endsWith(".md")) return false;
    const top = item.path.split("/")[0];
    return INCLUDE_EXACT.has(item.path) || INCLUDE_TOPS.has(top);
  });

  console.log(`Found ${mdBlobs.length} .md file(s)`);

  const manifest = { files: {}, synced_at: new Date().toISOString() };

  // Download each file and store individually in KV
  await Promise.all(
    mdBlobs.map(async (blob) => {
      const rawUrl = `https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}/${blob.path}`;
      const content = await fetchRaw(rawUrl, env.GITHUB_TOKEN);

      const kvKey = `file:${blob.path}`;
      await env.CONTEXTS_KV.put(kvKey, content, { expirationTtl: CACHE_TTL_SEC });

      manifest.files[blob.path] = {
        path: blob.path,
        size: content.length,
        // Content is also inlined for clients that want a single-request download
        content,
      };
    })
  );

  // Store manifest (includes all file content for one-shot client downloads)
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
