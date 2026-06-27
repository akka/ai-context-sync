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
    # GITHUB_REPO=akka/org-ai-contexts
    # GITHUB_BRANCH=main

  MODE 3 — Local copy (cowork / hook mode):
    Pass --local-copy to mirror ~/.claude/ into a project .claude/ directory
    without any network calls. Intended for use via a UserPromptSubmit hook
    so cowork sessions get org context at session start and refreshed every
    --cooldown minutes during long-lived sessions.

Installed layout:
  ~/.claude/
    CLAUDE.md                ← managed @import block prepended by this script
    contexts/
      index.md               ← navigation guide (what's available and when to use it)
      context/
        company.md
        platform.md
        ...
    skills/
      engineering/
        SKILL.md             ← activatable via /engineering
      infosec/
        ...
    commands/
      support-triage.md      ← slash command /support-triage

The script is idempotent — safe to run repeatedly via a scheduler.
"""

import argparse
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import urllib.error
import urllib.request
from typing import Optional, Tuple

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_GITHUB_REPO    = "akka/org-ai-contexts"
DEFAULT_GITHUB_BRANCH  = "main"
DEFAULT_COOLDOWN_MINS  = 360  # 6 hours — matches cron cadence

CLAUDE_DIR       = pathlib.Path.home() / ".claude"
CONTEXTS_DIR     = CLAUDE_DIR / "contexts"
SKILLS_DIR       = CLAUDE_DIR / "skills"
COMMANDS_DIR     = CLAUDE_DIR / "commands"
CLAUDE_MD        = CLAUDE_DIR / "CLAUDE.md"
CONFIG_FILE      = CLAUDE_DIR / "context-sync.conf"
LOG_FILE         = CLAUDE_DIR / "context-sync.log"
BACKUPS_DIR      = CLAUDE_DIR / "backups"
MAX_BACKUPS      = 5

CONTEXTS_SUBDIR  = "contexts"
TIMESTAMP_FILE   = ".sync-timestamp"

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


def resolve(key: str, cli_val: Optional[str], config: dict) -> Optional[str]:
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

# ── File classification ────────────────────────────────────────────────────────

def has_skill_frontmatter(content: str) -> bool:
    """Return True if the file has a YAML frontmatter block containing 'name:'."""
    if not content.startswith("---"):
        return False
    end = content.find("\n---", 3)
    if end == -1:
        return False
    frontmatter = content[3:end]
    return bool(re.search(r"^name:\s*\S", frontmatter, re.MULTILINE))


def classify_file(path: str, content: str) -> Optional[Tuple[str, pathlib.Path]]:
    """
    Return (kind, dest_path) for a repo path, or None to skip.

    Kinds:
      "context"  → ~/.claude/contexts/<path>       (always-on @import)
      "skill"    → ~/.claude/skills/<name>/SKILL.md
      "command"  → ~/.claude/commands/<stem>.md
    """
    parts = path.split("/")
    top   = parts[0]
    name  = pathlib.Path(path).stem

    # Root-level index.md only — navigation guide, always-on
    if path == "index.md":
        return ("context", CONTEXTS_DIR / "index.md")

    # context/ directory → always-on context
    if top == "context" and path.endswith(".md"):
        return ("context", CONTEXTS_DIR / pathlib.Path(path))

    # skills/ directory → ~/.claude/skills/<name>/SKILL.md (if has frontmatter)
    if top == "skills" and path.endswith(".md"):
        if name in ("overview", "README", "readme"):
            return None
        if not has_skill_frontmatter(content):
            log.debug("  skipping %s (no skill frontmatter)", path)
            return None
        return ("skill", SKILLS_DIR / name / "SKILL.md")

    # prompts/ directory → ~/.claude/commands/<name>.md
    if top == "prompts" and path.endswith(".md"):
        return ("command", COMMANDS_DIR / f"{name}.md")

    return None

# ── Backup ─────────────────────────────────────────────────────────────────────

def backup_existing_files(dry_run: bool) -> None:
    """
    Snapshot all files that this script manages into a timestamped backup dir,
    keeping the last MAX_BACKUPS snapshots.

    Covers: CLAUDE.md, contexts/, skills/, commands/
    """
    import datetime

    managed: list[pathlib.Path] = []
    if CLAUDE_MD.exists():
        managed.append(CLAUDE_MD)
    for managed_dir in (CONTEXTS_DIR, SKILLS_DIR, COMMANDS_DIR):
        if managed_dir.exists():
            managed.extend(p for p in managed_dir.rglob("*") if p.is_file())

    if not managed:
        log.debug("Nothing to back up.")
        return

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = BACKUPS_DIR / timestamp

    if dry_run:
        log.info("DRY RUN — would back up %d file(s) to %s", len(managed), backup_root)
        return

    backup_root.mkdir(parents=True, exist_ok=True)
    for src in managed:
        # Preserve relative path under ~/.claude/
        rel = src.relative_to(CLAUDE_DIR)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    log.debug("Backed up %d file(s) → %s", len(managed), backup_root)

    # Prune oldest backups, keep only MAX_BACKUPS (entries may be dirs or stray files)
    snapshots = sorted(BACKUPS_DIR.iterdir())
    for old in snapshots[:-MAX_BACKUPS]:
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()
        log.debug("Removed old backup %s", old)


# ── Install helpers ────────────────────────────────────────────────────────────

def install_file(path: str, content: str, dry_run: bool) -> Optional[Tuple[str, pathlib.Path]]:
    """Classify and write a single file. Returns (kind, dest) or None if skipped."""
    result = classify_file(path, content)
    if result is None:
        return None

    kind, dest = result
    log.info("  [%s] %s → %s", kind, path, dest)
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return kind, dest


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

    backup_existing_files(dry_run)

    context_paths: list[str] = []

    for path, meta in files.items():
        content: str = meta["content"]
        result = install_file(path, content, dry_run)
        if result and result[0] == "context":
            context_paths.append(path)

    update_claude_md(context_paths, dry_run)
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

    backup_existing_files(dry_run)

    context_paths: list[str] = []

    for blob in md_blobs:
        path: str = blob["path"]
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        log.info("  downloading %s", path)
        content = http_get(raw_url, {
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-context-sync/1.0",
        }).decode("utf-8")

        result = install_file(path, content, dry_run)
        if result and result[0] == "context":
            context_paths.append(path)

    update_claude_md(context_paths, dry_run)
    log.info("✓ Sync complete.")

# ── CLAUDE.md management ───────────────────────────────────────────────────────

def _context_import_path(repo_path: str) -> str:
    """Convert a repo path to the @import path relative to ~/.claude/."""
    if repo_path == "index.md":
        return f"{CONTEXTS_SUBDIR}/index.md"
    # repo path is like "context/company.md" → contexts/context/company.md
    return f"{CONTEXTS_SUBDIR}/{repo_path}"


def build_sync_block(context_paths: list[str]) -> str:
    lines = [
        SYNC_BLOCK_START,
        "# Company AI Contexts  (auto-synced — do not edit this section manually)",
        "",
    ]

    # index.md first if present
    if "index.md" in context_paths:
        lines.append(f"@{_context_import_path('index.md')}")

    # Group remaining by subdirectory
    subdir_map: dict[str, list[str]] = {}
    for path in sorted(context_paths):
        if path == "index.md":
            continue
        top = path.split("/")[0]
        subdir_map.setdefault(top, []).append(path)

    for subdir in sorted(subdir_map):
        lines.append(f"\n## {subdir.replace('-', ' ').replace('_', ' ').title()}")
        for path in sorted(subdir_map[subdir]):
            lines.append(f"@{_context_import_path(path)}")

    lines.append("")
    lines.append(SYNC_BLOCK_END)
    return "\n".join(lines) + "\n"


def update_claude_md(context_paths: list[str], dry_run: bool = False) -> None:
    block = build_sync_block(context_paths)

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

# ── Mode 3: Local copy (cowork hook) ──────────────────────────────────────────

def _is_within_cooldown(target_dir: pathlib.Path, cooldown_mins: int) -> bool:
    """Return True if a sync has already run within the cooldown window."""
    import time
    ts_file = target_dir / TIMESTAMP_FILE
    if not ts_file.exists():
        return False
    age_mins = (time.time() - ts_file.stat().st_mtime) / 60
    return age_mins < cooldown_mins


def _write_timestamp(target_dir: pathlib.Path) -> None:
    import time
    ts_file = target_dir / TIMESTAMP_FILE
    ts_file.write_text(str(time.time()), encoding="utf-8")


def sync_local_copy(target_dir: pathlib.Path, cooldown_mins: int, dry_run: bool) -> None:
    """
    Mirror ~/.claude/contexts/, ~/.claude/skills/, and ~/.claude/commands/ into
    <target_dir>/ and inject a managed @import block into <target_dir>/CLAUDE.md.

    Uses a cooldown timestamp so repeated hook invocations within a session are
    cheap (no-ops after the first copy until the cooldown expires).
    """
    if _is_within_cooldown(target_dir, cooldown_mins):
        log.info("Local copy skipped — within %d-minute cooldown window.", cooldown_mins)
        return

    if not CLAUDE_DIR.exists():
        raise SystemExit(
            f"[ERROR] ~/.claude/ not found. Run the full sync first to populate it."
        )

    log.info("Copying org context from %s → %s …", CLAUDE_DIR, target_dir)

    dirs_to_copy = [
        (CONTEXTS_DIR, target_dir / "contexts"),
        (SKILLS_DIR,   target_dir / "skills"),
        (COMMANDS_DIR, target_dir / "commands"),
    ]

    context_paths: list[str] = []

    for src_dir, dest_dir in dirs_to_copy:
        if not src_dir.exists():
            log.debug("  skipping %s (not present in ~/.claude/)", src_dir.name)
            continue
        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(CLAUDE_DIR)
            dest_file = target_dir / rel
            log.info("  %s", rel)
            if not dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
            # Collect context paths for CLAUDE.md import block
            if src_dir == CONTEXTS_DIR:
                # rel is like contexts/context/company.md — strip the leading "contexts/"
                context_rel = src_file.relative_to(CONTEXTS_DIR)
                path_str = str(context_rel).replace("\\", "/")
                if path_str == "index.md":
                    context_paths.append("index.md")
                else:
                    context_paths.append(f"context/{context_rel.name}" if len(context_rel.parts) == 2 else str(context_rel).replace("\\", "/"))

    # Rebuild CLAUDE.md import block in the target dir
    _update_target_claude_md(target_dir, context_paths, dry_run)

    if not dry_run:
        _write_timestamp(target_dir)

    log.info("✓ Local copy complete.")


def _update_target_claude_md(target_dir: pathlib.Path, context_paths: list[str], dry_run: bool) -> None:
    """Write the managed sync block into <target_dir>/CLAUDE.md."""
    target_claude_md = target_dir / "CLAUDE.md"

    # Rebuild import paths relative to the target .claude/ dir
    block_lines = [
        SYNC_BLOCK_START,
        "# Company AI Contexts  (auto-synced — do not edit this section manually)",
        "",
    ]

    if "index.md" in context_paths:
        block_lines.append(f"@contexts/index.md")

    subdir_map: dict[str, list[str]] = {}
    for path in sorted(context_paths):
        if path == "index.md":
            continue
        top = path.split("/")[0]
        subdir_map.setdefault(top, []).append(path)

    for subdir in sorted(subdir_map):
        block_lines.append(f"\n## {subdir.replace('-', ' ').replace('_', ' ').title()}")
        for path in sorted(subdir_map[subdir]):
            block_lines.append(f"@contexts/{path}")

    block_lines += ["", SYNC_BLOCK_END, ""]
    block = "\n".join(block_lines)

    if target_claude_md.exists():
        existing = target_claude_md.read_text(encoding="utf-8")
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
        log.info("DRY RUN — would write %s:\n%s", target_claude_md, new_content)
    else:
        target_claude_md.parent.mkdir(parents=True, exist_ok=True)
        target_claude_md.write_text(new_content, encoding="utf-8")
        log.info("%s %s", action, target_claude_md)


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
        "--local-copy", action="store_true",
        help="Copy from ~/.claude/ into --target-dir without any network calls. "
             "Intended for use in a UserPromptSubmit hook for cowork sessions.",
    )
    parser.add_argument(
        "--target-dir", metavar="DIR", default=".claude",
        help="Destination directory for --local-copy (default: .claude in CWD).",
    )
    parser.add_argument(
        "--cooldown", metavar="MINS", type=int, default=DEFAULT_COOLDOWN_MINS,
        help=f"Skip copy if last run was within this many minutes (default: {DEFAULT_COOLDOWN_MINS}). "
             "Use 0 to always copy.",
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

    if args.local_copy:
        target_dir = pathlib.Path(args.target_dir).expanduser().resolve()
        sync_local_copy(target_dir, cooldown_mins=args.cooldown, dry_run=args.dry_run)
        return

    source_url = resolve("SOURCE_URL",     args.source_url,   config)
    api_key    = resolve("CONTEXT_API_KEY", args.api_key,      config)
    gh_token   = resolve("GITHUB_TOKEN",   args.github_token, config)
    repo       = resolve("GITHUB_REPO",    args.repo,         config) or DEFAULT_GITHUB_REPO
    branch     = resolve("GITHUB_BRANCH",  args.branch,       config) or DEFAULT_GITHUB_BRANCH

    if not args.dry_run:
        for d in (CONTEXTS_DIR, SKILLS_DIR, COMMANDS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    if source_url:
        if not api_key:
            raise SystemExit(
                f"\n[ERROR] CONTEXT_API_KEY not set.\n"
                f"  Add  CONTEXT_API_KEY=<key>  to {CONFIG_FILE}\n"
                f"  or pass --api-key <key>\n"
            )
        sync_from_worker(source_url, api_key, dry_run=args.dry_run)
    elif gh_token:
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
