#!/usr/bin/env bash
#
# Start the yaml-bookmarks web UI.
#
# If an instance is already running for the current user, it is killed first, so
# this always launches the latest code. Runs in the foreground (Ctrl+C to stop);
# extra arguments are passed through, e.g.:
#
#   ./start.sh                 # start (opens a browser)
#   ./start.sh --no-browser    # start without opening a browser
#   ./start.sh --port 9000     # start on a different port
#
set -eo pipefail

# Run from the directory this script lives in (the repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Kill any instance owned by this user so we run fresh code. pkill never matches
# its own PID, and this script's command line doesn't contain the pattern, so
# this only stops actual running servers.
if pkill -u "$(id -u)" -f 'yaml-bookmarks web' 2>/dev/null; then
    echo "Stopped a running yaml-bookmarks instance."
    sleep 1  # let it release the port
fi

# Prefer the project's virtualenv if there is one.
if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    . .venv/bin/activate
fi

if ! command -v yaml-bookmarks >/dev/null 2>&1; then
    echo "error: 'yaml-bookmarks' not found on PATH." >&2
    echo "       Install it first, e.g.:  pip install -e ." >&2
    exit 1
fi

echo "Starting yaml-bookmarks..."
exec yaml-bookmarks web "$@"
