# Claude Context Sync

Distributes company-wide AI context files from a central GitHub repository to every
employee's Claude Code installation. Runs on a daily schedule and keeps cowork sessions
in sync automatically.

**Requirements:** Python 3.8+ only — no pip dependencies.

---

## Architecture

```
github.com/akka/ai-context-sync  (this repo — .claude/ subtree is the source)
            │
            │  daily cron (07:00 UTC)
            ▼
  Cloudflare Worker  ──  KV cache
  claude-contexts.akka.io
            │
            │  HTTPS  +  Bearer token  (shared API key)
            ▼
  Employee machines  →  ~/.claude/contexts/     (daily cron/timer, 08:00 local)
                        ~/.claude/skills/
                        ~/.claude/CLAUDE.md      (auto-updated with @imports)
                              │
                              │  local copy — no network
                              ▼
                        <session>/.claude/       (cowork watcher daemon)
```

- The **GitHub bot token** never leaves Cloudflare — stored as a Worker secret.
- Employees receive a **shared API key** (scoped only to this endpoint, easily rotated).
- No GitHub account required for employees.
- **Cowork sessions** are handled by a background watcher daemon that copies from `~/.claude/`
  into each new session's `.claude/` directory — no extra network calls.

---

## Content layout

Content lives in the `.claude/` subdirectory of this repo. Its structure mirrors
exactly what gets installed under `~/.claude/` on employee machines — no path
transformation is needed.

```
.claude/
├── contexts/
│   ├── index.md            ← loaded in every session
│   └── context/
│       ├── company.md
│       ├── platform.md
│       └── ...
├── skills/                 ← loaded based on directory/project context
│   ├── engineering/
│   │   └── SKILL.md
│   ├── marketing/
│   │   └── SKILL.md
│   └── ...
└── commands/               ← slash commands available in every session
    ├── support-triage.md
    └── ...
```

The Cloudflare Worker reads from `.claude/` in this repo, strips the `.claude/`
prefix, and serves paths like `contexts/index.md`, `skills/ciso/SKILL.md`, and
`commands/support-triage.md`. The sync script installs them directly to the same
relative path under `~/.claude/`.

---

## Part 1 — Deploy the Cloudflare Worker (IT / admin, one-time)

### Prerequisites

- Cloudflare account with the `akka.io` zone
- [Node.js](https://nodejs.org) and `npm` (for Wrangler CLI)
- A GitHub bot PAT with `contents: read` on `akka/ai-context-sync`
- A generated API key to distribute to employees:
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

```bash
cd worker/
wrangler deploy
curl -X POST https://claude-contexts.akka.io/sync \
  -H "Authorization: Bearer YOUR_CONTEXT_API_KEY"
```

---

## Part 2 — Install on employee machines

Employees need only the **API key** — no GitHub account.

### macOS / Linux

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

1. Downloads `sync_claude_contexts.py` and `watch_cowork_sessions.py` to `~/.claude/`
2. Writes `~/.claude/context-sync.conf` with the API key (mode `600` / user-only ACL)
3. Schedules a **daily context sync** at 08:00:
   - **macOS:** launchd plist `io.akka.claude-context-sync.plist`
   - **Linux:** systemd user timer `akka-claude-context-sync.timer` (falls back to crontab)
   - **Windows:** Task Scheduler task `ClaudeContextSync`
4. Installs and starts the **cowork session watcher daemon** (see below)
5. Injects a `SessionStart` hook into `~/.claude/settings.json` (for non-cowork sessions)
6. Runs an initial sync immediately

### Self-update

On every scheduled sync run, `sync_claude_contexts.py` fetches the latest version of
itself and `watch_cowork_sessions.py` from GitHub. If either file changed, it replaces
the local copy atomically — no manual reinstall needed when the scripts are updated.

### Employee config file

`~/.claude/context-sync.conf`:
```ini
SOURCE_URL=https://claude-contexts.akka.io
CONTEXT_API_KEY=<key from IT>
```

The API key can also be set via the `CONTEXT_API_KEY` environment variable.

---

## Cowork session support

Claude cowork sessions run in an isolated VM where `~/.claude/` is not accessible.
To get org context into cowork, a background watcher daemon monitors the session
directory and copies context files into each new session as it starts.

### How it works

1. The watcher daemon polls every 5 seconds for new session directories under:
   - **macOS:** `~/Library/Application Support/Claude/local-agent-mode-sessions/`
   - **Linux:** `~/.config/Claude/local-agent-mode-sessions/` (XDG_CONFIG_HOME honoured)
   - **Windows:** `%APPDATA%/Claude/local-agent-mode-sessions/`
2. When a new `local_<uuid>` session directory appears, it copies `contexts/`, `skills/`,
   and `commands/` from `~/.claude/` into `<session>/.claude/`
3. It also writes a `CLAUDE.md` with `@import` directives and a bootstrap notice that
   prompts the model to announce when context is available
4. On macOS, a second `WatchPaths` launchd trigger fires instantly when the directory
   changes — the polling daemon is the fallback

### Daemon setup per platform

| Platform | Mechanism | Service name |
|----------|-----------|--------------|
| macOS | launchd KeepAlive plist | `io.akka.claude-cowork-watcher` |
| macOS (fast trigger) | launchd WatchPaths plist | `io.akka.claude-cowork-watchpath` |
| Linux (systemd) | systemd user service | `akka-claude-cowork-watcher.service` |
| Linux (fallback) | cron `@reboot` + immediate background start | — |
| Windows | Task Scheduler at-logon task | `ClaudeCoworkWatcher` |

### Session bootstrap notice

When the watcher syncs a new session, it injects a notice into the session's `CLAUDE.md`
instructing the model to announce once that company context is loaded:

> Company context loaded — give the system a moment to catch up, then re-try anything
> that needs Akka knowledge or team skills.

This fires on the first response after context becomes available in the session.

### .gitignore recommendation

If your project uses version control, add these to `.gitignore` to avoid committing
ephemeral session files:

```gitignore
.claude/.sync-timestamp
.claude/contexts/
.claude/skills/
.claude/commands/
```

### Logs

| File | Contents |
|------|----------|
| `~/.claude/context-sync.log` | Daily sync output |
| `~/.claude/cowork-watcher.log` | Cowork watcher daemon output |

---

## Rotating the API key

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

## CLAUDE.md backups

Before every sync, the script backs up managed files to timestamped directories under
`~/.claude/backups/`, keeping the last 5 snapshots. To restore:

```bash
ls ~/.claude/backups/
cp ~/.claude/backups/<timestamp>/CLAUDE.md ~/.claude/CLAUDE.md
cp -r ~/.claude/backups/<timestamp>/contexts/ ~/.claude/contexts/
```

---

## Uninstalling

### macOS

```bash
# Remove schedule, script, and config (leaves context files and backups in place)
curl -fsSL https://raw.githubusercontent.com/akka/ai-context-sync/main/uninstall.sh | bash

# Also remove downloaded context files and CLAUDE.md backups
curl -fsSL https://raw.githubusercontent.com/akka/ai-context-sync/main/uninstall.sh | bash -s -- --purge
```

### Linux

```bash
# Stop and remove systemd units (if installed)
systemctl --user disable --now akka-claude-context-sync.timer akka-claude-cowork-watcher.service
rm -f ~/.config/systemd/user/akka-claude-context-sync.{service,timer}
rm -f ~/.config/systemd/user/akka-claude-cowork-watcher.service
systemctl --user daemon-reload

# Remove cron entries (if using cron fallback)
crontab -l | grep -v "sync_claude_contexts\|watch_cowork_sessions" | crontab -

# Remove scripts and config
rm -f ~/.claude/sync_claude_contexts.py ~/.claude/watch_cowork_sessions.py ~/.claude/context-sync.conf
```

### Windows

```powershell
Unregister-ScheduledTask -TaskName ClaudeContextSync  -Confirm:$false
Unregister-ScheduledTask -TaskName ClaudeCoworkWatcher -Confirm:$false
Remove-Item "$env:USERPROFILE\.claude\sync_claude_contexts.py"  -ErrorAction SilentlyContinue
Remove-Item "$env:USERPROFILE\.claude\watch_cowork_sessions.py" -ErrorAction SilentlyContinue
Remove-Item "$env:USERPROFILE\.claude\context-sync.conf"        -ErrorAction SilentlyContinue
```

---

## Script reference

### sync_claude_contexts.py

```
sync_claude_contexts.py [options]

  --source-url URL    Worker URL (overrides SOURCE_URL in config)
  --api-key KEY       Bearer key (overrides CONTEXT_API_KEY in config)
  --github-token TOK  Direct GitHub mode — admin/fallback only
  --repo OWNER/REPO   GitHub repo for direct mode
  --branch BRANCH     Branch for direct mode
  --local-copy        Copy from ~/.claude/ into --target-dir (no network, for SessionStart hook)
  --target-dir DIR    Destination for --local-copy (default: .claude in CWD)
  --cooldown MINS     Skip copy if last run was within N minutes (default: 360)
  --dry-run           Show what would be done without writing files
  -v, --verbose       Debug logging
```

### watch_cowork_sessions.py

```
watch_cowork_sessions.py [options]

  --once    Scan for unsynced sessions once and exit (used by macOS WatchPaths trigger)
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `HTTP 401` | API key wrong or missing — check `context-sync.conf` |
| `HTTP 404 /manifest.json` | Worker not deployed or KV not yet populated — run `POST /sync` |
| `Python 3.8+ not found` | Install from https://python.org/downloads |
| Contexts not loading in Claude Code | Check `~/.claude/CLAUDE.md` contains the `<!-- claude-context-sync -->` block |
| Cowork session has no context | Check `~/.claude/cowork-watcher.log` — is the daemon running? |
| macOS: sync daemon not running | `launchctl list \| grep akka` — check exit code; reload with `launchctl load -w ~/Library/LaunchAgents/io.akka.claude-context-sync.plist` |
| macOS: cowork watcher not running | `launchctl list \| grep akka-claude-cowork` — reload watcher and watchpath plists |
| Linux: systemd timer not running | `systemctl --user status akka-claude-context-sync.timer` |
| Linux: cowork watcher not running | `systemctl --user status akka-claude-cowork-watcher.service` |
| Windows: sync task not running | Task Scheduler → `ClaudeContextSync` → History tab |
| Windows: cowork watcher not running | Task Scheduler → `ClaudeCoworkWatcher` → History tab |
| Worker cron not firing | Cloudflare dashboard → Workers → `claude-context-sync` → Cron Triggers |
