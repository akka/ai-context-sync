#!/usr/bin/env python3
"""
watch_cowork_sessions.py

Watches for new Claude cowork session directories and copies org context
from ~/.claude/ into each session's .claude/ directory so the cowork VM
starts with skills and context available.

Runs as a background daemon via launchd (macOS). Poll interval is
intentionally short (30s) so new sessions get context before the user
sends their first prompt.

Structure watched:
  ~/Library/Application Support/Claude/local-agent-mode-sessions/
    <workspace-id>/
      <team-id>/
        local_<session-uuid>/    ← each cowork session
          .claude/               ← we populate this
          outputs/
"""

import logging
import os
import pathlib
import shutil
import sys
import time

# ── Paths ──────────────────────────────────────────────────────────────────────

CLAUDE_DIR   = pathlib.Path.home() / ".claude"
SESSIONS_DIR = pathlib.Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"

DIRS_TO_COPY = ["contexts", "skills", "commands"]
SENTINEL     = "contexts"          # presence of this subdir = already synced
POLL_SECS    = 30
LOG_FILE     = CLAUDE_DIR / "cowork-watcher.log"

# ── Logging ────────────────────────────────────────────────────────────────────

def configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

log = logging.getLogger(__name__)

# ── Core logic ─────────────────────────────────────────────────────────────────

def find_session_dirs() -> list[pathlib.Path]:
    """Return all local_<uuid> session directories under SESSIONS_DIR."""
    if not SESSIONS_DIR.exists():
        return []
    return [
        p for p in SESSIONS_DIR.rglob("local_*")
        if p.is_dir() and not p.name.startswith("local_agent")
    ]


def needs_sync(session_dir: pathlib.Path) -> bool:
    """Return True if this session's .claude/ hasn't been populated yet."""
    return not (session_dir / ".claude" / SENTINEL).exists()


def sync_into_session(session_dir: pathlib.Path) -> None:
    """Copy org context from ~/.claude/ into a cowork session's .claude/."""
    target = session_dir / ".claude"
    target.mkdir(exist_ok=True)

    copied = 0
    for subdir in DIRS_TO_COPY:
        src = CLAUDE_DIR / subdir
        if not src.exists():
            continue
        dst = target / subdir
        for src_file in src.rglob("*"):
            if not src_file.is_file():
                continue
            dst_file = dst / src_file.relative_to(src)
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            copied += 1

    # Inject managed @import block into session's CLAUDE.md
    _update_claude_md(target)

    log.info("Synced %d file(s) into %s", copied, session_dir.name)


def _update_claude_md(target_claude: pathlib.Path) -> None:
    """Write org context @import block into <session>/.claude/CLAUDE.md."""
    BLOCK_START = "<!-- claude-context-sync:start -->"
    BLOCK_END   = "<!-- claude-context-sync:end -->"

    import re

    lines = [BLOCK_START, "# Company AI Contexts  (auto-synced)", ""]

    contexts_dir = target_claude / "contexts"
    if (contexts_dir / "index.md").exists():
        lines.append("@contexts/index.md")

    for ctx_file in sorted((contexts_dir / "context").glob("*.md")) if (contexts_dir / "context").exists() else []:
        lines.append(f"@contexts/context/{ctx_file.name}")

    lines += [
        "",
        "# Session bootstrap",
        "At the start of every session, before responding to the user, silently read",
        "all files in .claude/skills/ and .claude/contexts/ to ensure skills and",
        "company context are fully loaded. Do not announce this unless asked.",
        "",
        BLOCK_END, "",
    ]
    block = "\n".join(lines)

    claude_md = target_claude / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if BLOCK_START in existing:
            import re
            content = re.sub(
                rf"{re.escape(BLOCK_START)}.*?{re.escape(BLOCK_END)}\n?",
                block,
                existing,
                flags=re.DOTALL,
            )
        else:
            content = block + "\n" + existing
    else:
        content = block

    claude_md.write_text(content, encoding="utf-8")


# ── Main loop ──────────────────────────────────────────────────────────────────

def main() -> None:
    configure_logging()
    log.info("Cowork session watcher started (polling every %ds)", POLL_SECS)
    log.info("Watching: %s", SESSIONS_DIR)

    if not CLAUDE_DIR.exists():
        log.error("~/.claude/ not found — run the full sync first. Exiting.")
        sys.exit(1)

    synced: set[pathlib.Path] = set()

    while True:
        try:
            for session_dir in find_session_dirs():
                if session_dir in synced:
                    continue
                if needs_sync(session_dir):
                    log.info("New session detected: %s", session_dir.name)
                    sync_into_session(session_dir)
                synced.add(session_dir)
        except Exception as exc:
            log.warning("Poll error: %s", exc)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
