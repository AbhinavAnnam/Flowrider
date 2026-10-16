#!/usr/bin/env bash
# One process serves the API and built frontend. No sudo or network cleanup.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
demo_mode="${1:-replay}"
if (( $# )); then shift; fi
case "$demo_mode" in replay|live) ;; *) echo 'Usage: bash scripts/start_demo.sh [replay|live] [--port 8080] [--live-db PATH]' >&2; exit 2;; esac
demo_python="${FLOWRIDER_PYTHON:-$HOME/.pyenv/versions/3.10.16/bin/python}"
if [[ ! -x "$demo_python" ]]; then
    echo "Controller Python not found: $demo_python. Set FLOWRIDER_PYTHON to the existing OS-Ken Python." >&2
    exit 1
fi
# Make a standalone SQLite backup, including committed WAL records, once.
# This preserves the original experiment and remains usable after WSL restarts.
if [[ ! -f data/demo-replay.sqlite && -f data/research-20261015T180515Z/inference-71.sqlite ]]; then
    "$demo_python" - <<'PY'
from pathlib import Path
import os, sqlite3, tempfile
source = Path('data/research-20261015T180515Z/inference-71.sqlite').resolve()
with tempfile.NamedTemporaryFile(dir='data', suffix='.sqlite', delete=False) as temporary:
    temporary_path = Path(temporary.name)
try:
    with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as recording:
        with sqlite3.connect(temporary_path) as backup:
            recording.backup(backup)
            backup.execute('PRAGMA journal_mode=DELETE')
    os.replace(temporary_path, 'data/demo-replay.sqlite')
finally:
    temporary_path.unlink(missing_ok=True)
print('Prepared standalone replay database from the validated recording.')
PY
fi
if [[ ! -d frontend/node_modules ]]; then
    npm --prefix frontend ci --no-audit --no-fund
fi
npm --prefix frontend run build
exec "$demo_python" demo_server.py --mode "$demo_mode" "$@"
