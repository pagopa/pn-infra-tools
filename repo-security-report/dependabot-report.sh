#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Please install Python 3 and retry." >&2
  exit 1
fi

exec python3 "${SCRIPT_DIR}/dependabot-report.py" "$@"
