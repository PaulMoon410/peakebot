#!/usr/bin/env bash
# start.sh — Start both the Node.js server and the Python knowledge server
# Usage: ./start.sh

set -e

# Optional: override via environment before running this script
export FTP_HOST="${FTP_HOST:-ftp.geocities.ws}"
export FTP_USER="${FTP_USER:-PeakeCoin}"
export FTP_PASSWORD="${FTP_PASSWORD:-Peake410}"
export PORT="${PORT:-3000}"
export PYTHON_PORT="${PYTHON_PORT:-5001}"
export PYTHON_KNOWLEDGE_SERVER="http://localhost:${PYTHON_PORT}"
export LLAMA_SERVER="${LLAMA_SERVER:-http://74.208.146.37:8080}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== PeakeBot Startup ==="
echo "Node.js  server  -> http://localhost:${PORT}"
echo "Python knowledge -> http://localhost:${PYTHON_PORT}"
echo "FTP host         -> ${FTP_HOST}"
echo ""

# Verify Python 3 is available
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Please install Python 3.10+."
  exit 1
fi

# Verify Node.js is available
if ! command -v node &>/dev/null; then
  echo "ERROR: node not found. Please install Node.js 14+."
  exit 1
fi

# Start Python knowledge server in background
echo "[start] Starting Python knowledge server (port ${PYTHON_PORT})..."
python3 "${SCRIPT_DIR}/knowledge_server.py" &
PYTHON_PID=$!

# Give Python a moment to start
sleep 1

# Start Node.js server in foreground
echo "[start] Starting Node.js server (port ${PORT})..."
node "${SCRIPT_DIR}/server.js" &
NODE_PID=$!

# Trap Ctrl+C / SIGTERM to kill both processes
cleanup() {
  echo ""
  echo "[start] Shutting down..."
  kill "$PYTHON_PID" 2>/dev/null || true
  kill "$NODE_PID" 2>/dev/null || true
  wait 2>/dev/null
  echo "[start] Done."
}
trap cleanup SIGINT SIGTERM

# Wait for either process to exit
wait -n 2>/dev/null || wait
cleanup
