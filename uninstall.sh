#!/usr/bin/env bash
# uninstall.sh — Remove claude-context-sync from macOS or Linux
#
# Usage:
#   bash uninstall.sh            # removes schedule, script, and config
#   bash uninstall.sh --purge    # also removes downloaded context files and backups

set -euo pipefail

PURGE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

CLAUDE_DIR="${HOME}/.claude"
SCRIPT_PATH="${CLAUDE_DIR}/sync_claude_contexts.py"
CONFIG_FILE="${CLAUDE_DIR}/context-sync.conf"
LOG_FILE="${CLAUDE_DIR}/context-sync.log"
CONTEXTS_DIR="${CLAUDE_DIR}/contexts"
BACKUPS_DIR="${CLAUDE_DIR}/backups"
CLAUDE_MD="${CLAUDE_DIR}/CLAUDE.md"
SYNC_BLOCK_START="<!-- claude-context-sync:start -->"
SYNC_BLOCK_END="<!-- claude-context-sync:end -->"

info()  { echo "  [INFO]  $*"; }
warn()  { echo "  [WARN]  $*" >&2; }

echo ""
echo "══════════════════════════════════════════"
echo "  Claude Context Sync — Uninstaller"
echo "══════════════════════════════════════════"
echo ""

OS="$(uname -s)"

# ── Remove scheduled job ──────────────────────────────────────────────────────
if [[ "$OS" == "Darwin" ]]; then
  PLIST="${HOME}/Library/LaunchAgents/io.akka.claude-context-sync.plist"
  if [[ -f "$PLIST" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    info "Removed launchd plist."
  else
    info "No launchd plist found — skipping."
  fi
elif [[ "$OS" == "Linux" ]]; then
  if crontab -l 2>/dev/null | grep -q "sync_claude_contexts"; then
    (crontab -l 2>/dev/null | grep -v "claude-context-sync\|sync_claude_contexts") | crontab -
    info "Removed cron entry."
  else
    info "No cron entry found — skipping."
  fi
fi

# ── Remove script and config ──────────────────────────────────────────────────
[[ -f "$SCRIPT_PATH" ]] && rm -f "$SCRIPT_PATH" && info "Removed ${SCRIPT_PATH}."
[[ -f "$CONFIG_FILE" ]] && rm -f "$CONFIG_FILE" && info "Removed ${CONFIG_FILE}."
[[ -f "$LOG_FILE"    ]] && rm -f "$LOG_FILE"    && info "Removed ${LOG_FILE}."

# ── Remove sync block from CLAUDE.md ─────────────────────────────────────────
if [[ -f "$CLAUDE_MD" ]] && grep -q "$SYNC_BLOCK_START" "$CLAUDE_MD"; then
  python3 - <<PYEOF
import re, pathlib
p = pathlib.Path("${CLAUDE_MD}")
content = p.read_text(encoding="utf-8")
cleaned = re.sub(
    r"${SYNC_BLOCK_START}.*?${SYNC_BLOCK_END}\n?",
    "",
    content,
    flags=re.DOTALL,
)
p.write_text(cleaned.lstrip("\n"), encoding="utf-8")
print("  [INFO]  Removed sync block from ${CLAUDE_MD}.")
PYEOF
fi

# ── Purge context files and backups ──────────────────────────────────────────
if [[ "$PURGE" == true ]]; then
  [[ -d "$CONTEXTS_DIR" ]] && rm -rf "$CONTEXTS_DIR" && info "Removed ${CONTEXTS_DIR}."
  [[ -d "$BACKUPS_DIR"  ]] && rm -rf "$BACKUPS_DIR"  && info "Removed ${BACKUPS_DIR}."
else
  warn "Context files and backups left in place. Run with --purge to remove them."
fi

echo ""
echo "  Done!"
echo ""
