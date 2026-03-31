#!/usr/bin/env bash
# install.sh — Install claude-context-sync on macOS or Linux
#
# Usage:
#   bash install.sh --key <CONTEXT_API_KEY>
#   bash install.sh --key <CONTEXT_API_KEY> --url https://claude-contexts.akka.io
#
# The API key is provided by IT/DevEx — it is NOT a GitHub token.
#
# Requirements: Python 3.8+

set -euo pipefail

SCRIPT_NAME="sync_claude_contexts.py"
SCRIPT_SRC="https://raw.githubusercontent.com/akka/ai-assistant-configs/main/${SCRIPT_NAME}"
INSTALL_DIR="${HOME}/.claude"
INSTALL_PATH="${INSTALL_DIR}/${SCRIPT_NAME}"
CONFIG_FILE="${INSTALL_DIR}/context-sync.conf"

CONTEXT_API_KEY=""
SOURCE_URL="https://claude-contexts.akka.io"

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --key|-k)   CONTEXT_API_KEY="$2"; shift 2 ;;
    --url|-u)   SOURCE_URL="$2";      shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
info()  { echo "  [INFO]  $*"; }
warn()  { echo "  [WARN]  $*" >&2; }
error() { echo "  [ERROR] $*" >&2; exit 1; }

check_python() {
  for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
      ver=$("$cmd" --version 2>&1 | awk '{print $2}')
      major=$(echo "$ver" | cut -d. -f1)
      minor=$(echo "$ver" | cut -d. -f2)
      if [[ "$major" -ge 3 && "$minor" -ge 8 ]]; then
        echo "$cmd"
        return
      fi
    fi
  done
  error "Python 3.8+ is required but not found. Install from https://python.org"
}

# ── Main ─────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  Claude Context Sync — Installer"
echo "══════════════════════════════════════════"
echo ""

PYTHON=$(check_python)
info "Using Python: $($PYTHON --version)"

# On macOS, Python.org installs ship without system certificates.
# Run the bundled Install Certificates script if present.
if [[ "${OS:-}" == "Darwin" ]] || [[ "$(uname -s)" == "Darwin" ]]; then
  PYTHON_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  CERT_CMD="/Applications/Python ${PYTHON_VER}/Install Certificates.command"
  if [[ -f "$CERT_CMD" ]]; then
    info "Installing Python certificates (macOS)…"
    bash "$CERT_CMD" > /dev/null 2>&1 && info "Certificates installed." || warn "Certificate install failed — SSL errors may occur."
  fi
fi

mkdir -p "${INSTALL_DIR}"

# Download (or copy if running locally) the sync script
if [[ -f "./${SCRIPT_NAME}" ]]; then
  info "Copying ${SCRIPT_NAME} from current directory…"
  cp "./${SCRIPT_NAME}" "${INSTALL_PATH}"
else
  info "Downloading ${SCRIPT_NAME}…"
  if command -v curl &>/dev/null; then
    curl -fsSL "${SCRIPT_SRC}" -o "${INSTALL_PATH}"
  elif command -v wget &>/dev/null; then
    wget -q "${SCRIPT_SRC}" -O "${INSTALL_PATH}"
  else
    error "Neither curl nor wget found. Please download ${SCRIPT_NAME} manually."
  fi
fi
chmod +x "${INSTALL_PATH}"
info "Installed script to ${INSTALL_PATH}"

# Write config file
if [[ -n "${CONTEXT_API_KEY}" ]]; then
  cat > "${CONFIG_FILE}" << EOF
SOURCE_URL=${SOURCE_URL}
CONTEXT_API_KEY=${CONTEXT_API_KEY}
EOF
  chmod 600 "${CONFIG_FILE}"
  info "Saved config to ${CONFIG_FILE} (mode 600)"
elif [[ ! -f "${CONFIG_FILE}" ]]; then
  cat > "${CONFIG_FILE}" << EOF
# Claude Context Sync configuration
# Contact IT for your CONTEXT_API_KEY — do NOT share it.
SOURCE_URL=${SOURCE_URL}
CONTEXT_API_KEY=YOUR_KEY_HERE
EOF
  chmod 600 "${CONFIG_FILE}"
  warn "API key not provided — edit ${CONFIG_FILE} and set CONTEXT_API_KEY."
fi

# ── Schedule ─────────────────────────────────────────────────────────────────
OS="$(uname -s)"

if [[ "$OS" == "Darwin" ]]; then
  PLIST_DIR="${HOME}/Library/LaunchAgents"
  PLIST_FILE="${PLIST_DIR}/io.akka.claude-context-sync.plist"
  mkdir -p "${PLIST_DIR}"
  PYTHON_ABS=$(command -v "${PYTHON}")

  cat > "${PLIST_FILE}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.akka.claude-context-sync</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_ABS}</string>
    <string>${INSTALL_PATH}</string>
  </array>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>${INSTALL_DIR}/context-sync.log</string>
  <key>StandardErrorPath</key>
  <string>${INSTALL_DIR}/context-sync.log</string>

  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
EOF

  launchctl unload "${PLIST_FILE}" 2>/dev/null || true
  launchctl load -w "${PLIST_FILE}"
  info "Scheduled via launchd — runs daily at 08:00. Plist: ${PLIST_FILE}"

elif [[ "$OS" == "Linux" ]]; then
  PYTHON_ABS=$(command -v "${PYTHON}")
  CRON_LINE="0 8 * * * ${PYTHON_ABS} ${INSTALL_PATH} >> ${INSTALL_DIR}/context-sync.log 2>&1"
  (crontab -l 2>/dev/null | grep -v "claude-context-sync\|sync_claude_contexts" ; echo "${CRON_LINE}") | crontab -
  info "Scheduled via cron — runs daily at 08:00."
else
  warn "Unrecognised OS '${OS}' — skipping scheduler setup. Schedule manually."
fi

# ── First run ─────────────────────────────────────────────────────────────────
if [[ -n "${CONTEXT_API_KEY}" ]]; then
  echo ""
  info "Running initial sync…"
  "${PYTHON}" "${INSTALL_PATH}"
else
  echo ""
  echo "  Next steps:"
  echo "  1. Edit ${CONFIG_FILE} and set CONTEXT_API_KEY=<key from IT>"
  echo "  2. Run manually once to verify:  ${PYTHON} ${INSTALL_PATH}"
fi

echo ""
echo "  Done!  Logs: ${INSTALL_DIR}/context-sync.log"
echo ""
