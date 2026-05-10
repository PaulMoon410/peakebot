#!/usr/bin/env bash
# start.sh — Start the Node.js server (which spawns Python knowledge server as subprocess)
# Usage: ./start.sh

set -e

# Optional: override via environment before running this script
export FTP_HOST="${FTP_HOST:-ftp.geocities.ws}"
export FTP_USER="${FTP_USER:-PeakeCoin}"
export FTP_PASSWORD="${FTP_PASSWORD:-Peake410}"
export PORT="${PORT:-3000}"
export PYTHON_PORT="${PYTHON_PORT:-5001}"
export LLAMA_SERVER="${LLAMA_SERVER:-http://74.208.146.37:8080}"
export START_PYTHON_SERVER="${START_PYTHON_SERVER:-true}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== PeakeBot Startup ==="
echo "Node.js server   -> http://localhost:${PORT}"
echo "Python subprocess-> http://localhost:${PYTHON_PORT}"
echo "FTP host         -> ${FTP_HOST}"
echo ""

# Verify Node.js is available
if ! command -v node &>/dev/null; then
  echo "ERROR: node not found. Please install Node.js 14+."
  exit 1
fi

# Warn if Python is missing (but don't fail; Node will handle it)
if [[ "${START_PYTHON_SERVER}" != "false" ]] && ! command -v python3 &>/dev/null; then
  echo "WARNING: python3 not found. Python knowledge server will not start."
  export START_PYTHON_SERVER=false
fi

# Start Node.js server in foreground (it will spawn Python as a subprocess)
echo "[start] Starting Node.js server (port ${PORT})..."
echo "[start] (Node will spawn Python knowledge server on port ${PYTHON_PORT})"
node "${SCRIPT_DIR}/server.js"
