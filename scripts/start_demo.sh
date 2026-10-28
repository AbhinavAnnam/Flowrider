#!/usr/bin/env bash
# One process serves the API and built frontend. No sudo or network cleanup.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
demo_mode="${1:-replay}"
if (( $# )); then shift; fi
case "$demo_mode" in replay|live) ;; *) echo 'Usage: bash scripts/start_demo.sh [replay|live] [--port 8080] [--live-db PATH]' >&2; exit 2;; esac
if [[ -n "${FLOWRIDER_PYTHON:-}" ]]; then
    demo_python="$FLOWRIDER_PYTHON"
elif [[ -x .venv/bin/python ]]; then
    demo_python="$PWD/.venv/bin/python"
else
    demo_python="python3"
fi
if ! command -v "$demo_python" >/dev/null 2>&1; then
    echo "Python not found: $demo_python. Install Python 3.10+ or set FLOWRIDER_PYTHON." >&2
    exit 1
fi
if ! "$demo_python" -c 'import networkx' >/dev/null 2>&1; then
    echo "Missing demo dependencies. Run: $demo_python -m pip install -r requirements-demo.txt" >&2
    exit 1
fi
for demo_asset in data/demo-replay.sqlite data/demo-dashboard/six-switch.json; do
    if [[ ! -f "$demo_asset" ]]; then
        echo "Missing demo asset: $demo_asset. Its dashboard view will show unavailable; restore the tracked asset from Git." >&2
    fi
done
if ! command -v npm >/dev/null 2>&1; then
    echo 'npm is required. Install Node.js 22.12+ and npm; see README.md.' >&2
    exit 1
fi
if [[ ! -d frontend/node_modules ]]; then
    npm --prefix frontend ci --no-audit --no-fund
fi
npm --prefix frontend run build
exec "$demo_python" demo_server.py --mode "$demo_mode" "$@"
