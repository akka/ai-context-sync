#!/usr/bin/env python3
"""
sync_claude_contexts.py

Fetches company AI context files and installs them into Claude Code's context
hierarchy (~/.claude/).

Two modes — configure ONE in ~/.claude/context-sync.conf:

  MODE 1 — Cloudflare Worker (recommended, no GitHub account needed):
    SOURCE_URL=https://claude-contexts.akka.io
    CONTEXT_API_KEY=<shared key from IT>

  MODE 2 — Direct GitHub access (admin / fallback):
    GITHUB_TOKEN=<bot PAT with contents:read>
    # GITHUB_REPO=akka/ai-assistant-configs
    # GITHUB_BRANCH=main

Installed layout:
  ~/.claude/
    CLAUDE.md                ← managed @import block prepended by this script
    contexts/
      company.md
      marketing/
        context.md
      support/
        context.md
      ...

The script is idempotent — safe to run repeatedly via a scheduler.
"""

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_GITHUB_REPO   = "akka/ai-assistant-configs"
DEFAULT_GITHUB_BRANCH = "main"

CLAUDE_DIR       = pathlib.Path.home() / ".claude"
CONTEXTS_SUBDIR  = "contexts"
CONTEXTS_DIR     = CLAUDE_DIR / CONTEXTS_SUBDIR
CLAUDE_MD        = CLAUDE_DIR / "CLAUDE.md"
CONFIG_FILE      = CLAUDE_DIR / "context-sync.conf"
LOG_FILE         = CLAUDE_DIR / "context-sync.log"

SYNC_BLOCK_START = "<!-- claude-context-sync:start -->"
SYNC_BLOCK_END   = "<!-- claude-context-sync:end -->"

# ── Logging ────────────────────────────────────────────────────────────────────

def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

log = logging.getLogger(__name__)

# ── Config file parsing ────────────────────────────────────────────────────────

def read_config() -> dict[str, str]:
    """Read key=value pairs from ~/.claude/context-sync.conf."""
    config: dict[str, str] = {}
    if not CONFIG_FILE.exists():
        return config
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            config[key.strip()] = val.strip()
    return config


def resolve(key: str, cli_val: str | None, config: dict[str, str]) -> str | None:
    """Priority: CLI arg > environment variable > config file."""
    if cli_val:
        return cli_val
    env = os.environ.get(key)
    if env:
        return env
    return config.get(key)

# ── HTTP helper ────────────────────────────────────────────────────────────────

def http_get(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"[ERROR] HTTP {exc.code} for {url}\n  {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"[ERROR] Network error: {exc.reason}") from exc

# ── Mode 1: Cloudflare Worker ──────────────────────────────────────────────────

def sync_from_worker(source_url: str, api_key: str, dry_run: bool) -> None:
    """Download manifest from the Cloudflare Worker and install contexts."""
    url = source_url.rstrip("/") + "/manifest.json"
    log.info("Fetching manifest from %s…", url)

    raw = http_get(url, {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "claude-context-sync/1.0",
    })
    manifest = json.loads(raw.decode("utf-8"))
    files: dict[str, dict] = manifest.get("files", {})

    if not files:
        log.warning("Manifest is empty — nothing to install.")
        return

    log.info("Manifest contains %d file(s), synced at %s", len(files), manifest.get("synced_at", "?"))

    root_paths: list[str] = []
    dept_map: dict[str, list[str]] = {}

    for path, meta in files.items():
        content: str = meta["content"]
        parts = path.split("/")

        if len(parts) == 1:
            root_paths.append(path)
            dest = CONTEXTS_DIR / path
        else:
            dept = parts[0]
            dept_map.setdefault(dept, []).append(path)
            filename = pathlib.Path(path).name
            dest = CONTEXTS_DIR / dept / filename

        log.info("  installing %s → %s", path, dest)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    update_claude_md(root_paths, dept_map, dry_run)
    log.info("✓ Sync complete.")

# ── Mode 2: Direct GitHub ──────────────────────────────────────────────────────

def sync_from_github(
    token: str,
    repo: str,
    branch: str,
    dry_run: bool,
) -> None:
    """Fetch directly from GitHub API — for admins / fallback only."""
    gh_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "claude-context-sync/1.0",
    }

    log.info("Fetching file tree from github.com/%s (branch: %s)…", repo, branch)
    raw = http_get(
        f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1",
        gh_headers,
    )
    tree_data = json.loads(raw.decode("utf-8"))

    if tree_data.get("truncated"):
        log.warning("GitHub tree response was truncated — some files may be missing.")

    md_blobs = [
        item for item in tree_data["tree"]
        if item["type"] == "blob" and item["path"].endswith(".md")
    ]

    if not md_blobs:
        log.warning("No .md files found in repository — nothing to sync.")
        return

    log.info("Found %d .md file(s)", len(md_blobs))

    root_paths: list[str] = []
    dept_map: dict[str, list[str]] = {}

    for blob in md_blobs:
        path: str = blob["path"]
        raw_url = (
            f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        )
        log.info("  downloading %s", path)
        content_bytes = http_get(raw_url, {"Authorization": f"Bearer {token}",
                                            "User-Agent": "claude-context-sync/1.0"})
        content = content_bytes.decode("utf-8")

        parts = path.split("/")
        if len(parts) == 1:
            root_paths.append(path)
            dest = CONTEXTS_DIR / path
        else:
            dept = parts[0]
            dept_map.setdefault(dept, []).append(path)
            filename = pathlib.Path(path).name
            dest = CONTEXTS_DIR / dept / filename

        log.info("    → %s", dest)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    update_claude_md(root_paths, dept_map, dry_run)
    log.info("✓ Sync complete.")

# ── CLAUDE.md management ───────────────────────────────────────────────────────

def build_sync_block(root_paths: list[str], dept_map: dict[str, list[str]]) -> str:
    lines = [
        SYNC_BLOCK_START,
        "# Company AI Contexts  (auto-synced — do not edit this section manually)",
        "",
    ]
    for path in sorted(root_paths):
        lines.append(f"@{CONTEXTS_SUBDIR}/{path}")

    for dept in sorted(dept_map):
        lines.append(f"\n## {dept.replace('-', ' ').replace('_', ' ').title()}")
        for path in sorted(dept_map[dept]):
            filename = pathlib.Path(path).name
            lines.append(f"@{CONTEXTS_SUBDIR}/{dept}/{filename}")

    lines.append("")
    lines.append(SYNC_BLOCK_END)
    return "\n".join(lines) + "\n"


def update_claude_md(
    root_paths: list[str],
    dept_map: dict[str, list[str]],
    dry_run: bool = False,
) -> None:
    block = build_sync_block(root_paths, dept_map)

    if CLAUDE_MD.exists():
        existing = CLAUDE_MD.read_text(encoding="utf-8")
        if SYNC_BLOCK_START in existing:
            new_content = re.sub(
                rf"{re.escape(SYNC_BLOCK_START)}.*?{re.escape(SYNC_BLOCK_END)}\n?",
                block,
                existing,
                flags=re.DOTALL,
            )
            action = "Updated"
        else:
            new_content = block + "\n" + existing
            action = "Prepended to"
    else:
        new_content = block
        action = "Created"

    if dry_run:
        log.info("DRY RUN — would write %s:\n%s", CLAUDE_MD, new_content)
    else:
        CLAUDE_MD.write_text(new_content, encoding="utf-8")
        log.info("%s %s", action, CLAUDE_MD)

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync company Claude context files to ~/.claude/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source-url", metavar="URL",
        help="Cloudflare Worker URL (e.g. https://claude-contexts.akka.io). "
             "Overrides env SOURCE_URL / config file.",
    )
    parser.add_argument(
        "--api-key", metavar="KEY",
        help="API key for the Worker endpoint. Overrides env CONTEXT_API_KEY / config file.",
    )
    parser.add_argument(
        "--github-token", metavar="TOKEN",
        help="GitHub PAT — only needed for direct GitHub mode. "
             "Overrides env GITHUB_TOKEN / config file.",
    )
    parser.add_argument(
        "--repo", metavar="OWNER/REPO", default=None,
        help=f"GitHub repo (direct mode, default: {DEFAULT_GITHUB_REPO})",
    )
    parser.add_argument(
        "--branch", metavar="BRANCH", default=None,
        help=f"GitHub branch (direct mode, default: {DEFAULT_GITHUB_BRANCH})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing any files",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    configure_logging(args.verbose)
    config = read_config()

    source_url = resolve("SOURCE_URL",     args.source_url,   config)
    api_key    = resolve("CONTEXT_API_KEY", args.api_key,      config)
    gh_token   = resolve("GITHUB_TOKEN",   args.github_token, config)
    repo       = resolve("GITHUB_REPO",    args.repo,         config) or DEFAULT_GITHUB_REPO
    branch     = resolve("GITHUB_BRANCH",  args.branch,       config) or DEFAULT_GITHUB_BRANCH

    if not args.dry_run:
        CONTEXTS_DIR.mkdir(parents=True, exist_ok=True)

    if source_url:
        # Worker mode — preferred for employees
        if not api_key:
            raise SystemExit(
                f"\n[ERROR] CONTEXT_API_KEY not set.\n"
                f"  Add  CONTEXT_API_KEY=<key>  to {CONFIG_FILE}\n"
                f"  or pass --api-key <key>\n"
            )
        sync_from_worker(source_url, api_key, dry_run=args.dry_run)
    elif gh_token:
        # Direct GitHub mode — for admins / fallback
        log.warning(
            "Using direct GitHub mode. For employees, configure SOURCE_URL + CONTEXT_API_KEY instead."
        )
        sync_from_github(gh_token, repo, branch, dry_run=args.dry_run)
    else:
        raise SystemExit(
            f"\n[ERROR] No sync source configured.\n"
            f"  Add to {CONFIG_FILE}:\n"
            f"    SOURCE_URL=https://claude-contexts.akka.io\n"
            f"    CONTEXT_API_KEY=<key from IT>\n"
        )


if __name__ == "__main__":
    main()
