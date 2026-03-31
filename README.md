# Claude Context Sync

Distributes company-wide AI context files from a central GitHub repository to every
employee's Claude Code installation.  Runs on a daily schedule.

**Requirements:** Python 3.8+ only — no pip dependencies.

---

## Architecture

```
github.com/akka/ai-assistant-configs  (private repo, bot token)
            │
            │  daily cron (07:00 UTC)
            ▼
  Cloudflare Worker  ──  KV cache
  claude-contexts.akka.io
            │
            │  HTTPS  +  Bearer token  (shared API key)
            ▼
  Employee machines  →  ~/.claude/contexts/
                        ~/.claude/CLAUDE.md  (auto-updated)
```

- The **GitHub bot token** never leaves Cloudflare — stored as a Worker secret.
- Employees receive a **shared API key** (scoped only to this endpoint, easily rotated).
- No GitHub account required for employees.

---

## GitHub repo layout

```
ai-contexts/
├── company.md          ← company-wide context (always loaded)
├── marketing/
│   └── context.md
├── support/
│   └── context.md
└── sales/
    └── context.md
```

Files are installed to `~/.claude/contexts/` and imported via `@` directives in
`~/.claude/CLAUDE.md`, giving Claude Code a proper hierarchy.

---

## Part 1 — Deploy the Cloudflare Worker (IT / admin, one-time)

### Prerequisites

- Cloudflare account with the `akka.io` zone
- [Node.js](https://nodejs.org) and `npm` (for Wrangler CLI)
- A GitHub bot PAT with `contents: read` on `akka/ai-assistant-configs`
- A generated API key to distribute to employees — generate one with:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

### Steps

```bash
cd worker/

# 1. Install Wrangler
npm install -g wrangler
wrangler login

# 2. Set up wrangler.toml
cp wrangler.toml.example wrangler.toml
# → Fill in the KV namespace ID — find it in 1Password: "ai-context-sync token and stuff"
# → Or create a fresh namespace: wrangler kv namespace create CONTEXTS_KV

# 3. Store secrets (never committed to git)
wrangler secret put GITHUB_TOKEN      # paste the bot PAT
wrangler secret put CONTEXT_API_KEY   # paste the shared employee key

# 4. Deploy
wrangler deploy

# 5. Test health endpoint (no auth required)
curl https://claude-contexts.akka.io/health

# 6. Trigger first sync immediately
curl -X POST https://claude-contexts.akka.io/sync \
  -H "Authorization: Bearer YOUR_CONTEXT_API_KEY"
```

**DNS setup:** Add a CNAME in the Cloudflare dashboard for `akka.io`:
```
claude-contexts.akka.io  →  claude-context-sync.lightbend.workers.dev
```

### Worker endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | None | Last sync timestamp — useful for monitoring |
| `GET /manifest.json` | Bearer key | Full manifest with all context file content |
| `GET /contexts/<path>` | Bearer key | Single context file |
| `POST /sync` | Bearer key | Manual sync trigger (GitHub → KV) |

The Worker syncs automatically via cron at **07:00 UTC** daily (before employees'
machines sync at 08:00 local time).

### Updating the Worker

After changing `worker/index.js` or `worker/wrangler.toml`, redeploy and trigger a manual sync:

```bash
cd worker/
wrangler deploy

# Repopulate KV from the (possibly updated) GitHub repo
curl -X POST https://claude-contexts.akka.io/sync \
  -H "Authorization: Bearer YOUR_CONTEXT_API_KEY"
```

---

## Part 2 — Install on employee machines

Employees need only the **API key** — no GitHub account.

### macOS

```bash
curl -fsSL https://raw.githubusercontent.com/akka/ai-context-sync/main/install.sh \
  | bash -s -- --key YOUR_CONTEXT_API_KEY
```

### Linux

```bash
curl -fsSL https://raw.githubusercontent.com/akka/ai-context-sync/main/install.sh \
  | bash -s -- --key YOUR_CONTEXT_API_KEY
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/akka/ai-context-sync/main/install.ps1" `
  -OutFile install.ps1 -UseBasicParsing
.\install.ps1 -Key "YOUR_CONTEXT_API_KEY"
```

> **Execution policy:** If blocked, run first:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### What the installer does

1. Downloads `sync_claude_contexts.py` to `~/.claude/`
2. Writes `~/.claude/context-sync.conf` with the API key (mode `600` / user-only ACL)
3. Schedules a daily job:
   - **macOS:** launchd plist at `~/Library/LaunchAgents/io.akka.claude-context-sync.plist`
   - **Linux:** user crontab entry
   - **Windows:** Task Scheduler task `ClaudeContextSync` with `StartWhenAvailable`
4. Runs an initial sync immediately

### Employee config file

`~/.claude/context-sync.conf`:
```ini
SOURCE_URL=https://claude-contexts.akka.io
CONTEXT_API_KEY=<key from IT>
```

The API key can also be set via the `CONTEXT_API_KEY` environment variable.

---

## Rotating the API key

When you need to issue a new key (employee offboarding, key exposure, periodic rotation):

```bash
cd worker/

# 1. Generate a new key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Update the Worker secret
wrangler secret put CONTEXT_API_KEY   # paste new key

# 3. Distribute the new key to employees via IT helpdesk / MDM
#    Employees update ~/.claude/context-sync.conf
#    or re-run the installer with the new key
```

The old key stops working immediately after step 2.

---

## Manual sync (any employee)

```bash
# macOS / Linux
python3 ~/.claude/sync_claude_contexts.py

# Windows
python "%USERPROFILE%\.claude\sync_claude_contexts.py"
```

---

## Removing the schedule

**macOS:**
```bash
launchctl unload ~/Library/LaunchAgents/io.akka.claude-context-sync.plist
rm ~/Library/LaunchAgents/io.akka.claude-context-sync.plist
```

**Linux:**
```bash
crontab -l | grep -v "sync_claude_contexts" | crontab -
```

**Windows:**
```powershell
Unregister-ScheduledTask -TaskName ClaudeContextSync -Confirm:$false
```

---

## Script reference

```
sync_claude_contexts.py [options]

  --source-url URL    Worker URL (overrides SOURCE_URL in config)
  --api-key KEY       Bearer key (overrides CONTEXT_API_KEY in config)
  --github-token TOK  Direct GitHub mode — admin/fallback only
  --repo OWNER/REPO   GitHub repo for direct mode
  --branch BRANCH     Branch for direct mode
  --dry-run           Show what would be done without writing files
  -v, --verbose       Debug logging
```

Logs: `~/.claude/context-sync.log`

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `HTTP 401` | API key wrong or missing — check `context-sync.conf` |
| `HTTP 404 /manifest.json` | Worker not deployed, or KV not yet populated — run `POST /sync` |
| `Python 3.8+ not found` | Install from https://python.org/downloads |
| Contexts not loading in Claude | Check `~/.claude/CLAUDE.md` contains the `<!-- claude-context-sync -->` block |
| macOS: launchd not running | `launchctl list \| grep akka` — check exit code in log |
| Windows: task not running | Task Scheduler → `ClaudeContextSync` → History tab |
| Worker cron not firing | Cloudflare dashboard → Workers → `claude-context-sync` → Cron Triggers |
