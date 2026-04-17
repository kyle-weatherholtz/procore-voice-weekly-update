#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.procore.voice-weekly-update"
PLIST_SRC="${SCRIPT_DIR}/${PLIST_NAME}.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="${HOME}/Library/Logs"

echo "=== Procore Voice Weekly Update — Setup ==="
echo ""

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install from https://python.org or via brew install python."
  exit 1
fi
echo "✓ Python3: $(python3 --version)"

# 2. Install Python dependencies
echo "Installing Python dependencies..."
pip3 install --quiet anthropic requests
echo "✓ anthropic + requests installed"

# 3. Check config.json
if [[ ! -f "${SCRIPT_DIR}/config.json" ]]; then
  echo ""
  echo "config.json not found. Copying config.example.json → config.json"
  cp "${SCRIPT_DIR}/config.example.json" "${SCRIPT_DIR}/config.json"
  echo ""
  echo "ACTION REQUIRED: Fill in your tokens in config.json:"
  echo "  atlassian_api_token  — https://id.atlassian.net/manage-profile/security/api-tokens"
  echo "  anthropic_api_key    — https://console.anthropic.com/settings/keys"
  echo ""
  echo "Then re-run: bash setup.sh"
  exit 1
else
  echo "✓ config.json found"
fi

# 4. Validate config.json has tokens filled in
PLACEHOLDER_CHECK=$(python3 -c "
import json, sys
with open('${SCRIPT_DIR}/config.json') as f:
    c = json.load(f)
issues = []
if '<' in c.get('atlassian_api_token', ''):
    issues.append('atlassian_api_token still has placeholder value')
if '<' in c.get('anthropic_api_key', ''):
    issues.append('anthropic_api_key still has placeholder value')
if issues:
    print('\\n'.join(issues))
    sys.exit(1)
" 2>&1 || true)

if [[ -n "${PLACEHOLDER_CHECK}" ]]; then
  echo ""
  echo "ERROR: config.json has unfilled values:"
  echo "  ${PLACEHOLDER_CHECK}"
  echo ""
  echo "Edit config.json and re-run: bash setup.sh"
  exit 1
fi
echo "✓ config.json tokens are set"

# 5. Create log directory
mkdir -p "${LOG_DIR}"
echo "✓ Log dir: ${LOG_DIR}"

# 6. Register launchd plist
if [[ ! -f "${PLIST_SRC}" ]]; then
  echo "ERROR: ${PLIST_SRC} not found"
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents"
cp "${PLIST_SRC}" "${PLIST_DEST}"

# Unload if already loaded (ignore errors)
launchctl unload "${PLIST_DEST}" 2>/dev/null || true

launchctl load "${PLIST_DEST}"
echo "✓ launchd job registered: ${PLIST_NAME}"

echo ""
echo "=== Setup complete ==="
echo ""
echo "The job will run every Friday at 4:00 PM."
echo ""
echo "Test now with:"
echo "  python3 ${SCRIPT_DIR}/weekly_update.py --dry-run --since $(date -v-7d +%Y-%m-%d)"
echo ""
echo "Logs:"
echo "  stdout: ${LOG_DIR}/procore-voice-update.log"
echo "  stderr: ${LOG_DIR}/procore-voice-update-error.log"
