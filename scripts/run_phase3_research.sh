#!/usr/bin/env bash
# Run in your own normal-user terminal; sudo authenticates Mininet normally.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
controller_python="${FLOWRIDER_PYTHON:-$HOME/.pyenv/versions/3.10.16/bin/python}"
session_seconds="${FLOWRIDER_SESSION_SECONDS:-600}"
controller_port="${FLOWRIDER_CONTROLLER_PORT:-16653}"
run_dir="${1:-data/research-$(date -u +%Y%m%dT%H%M%SZ)}"
if [[ -e "$run_dir" ]]; then
    echo "Refusing to overwrite existing experiment: $run_dir" >&2
    exit 1
fi
command -v iperf3 >/dev/null
"$controller_python" -c 'import os_ken, sklearn, matplotlib'
/usr/bin/python3 -c 'import mininet'
# Check the reserved experiment port without connecting to another controller.
"$controller_python" - "$controller_port" <<'PY'
import socket, sys
with socket.socket() as listener:
    listener.bind(('127.0.0.1', int(sys.argv[1])))
PY
sudo -v
mkdir -p -- "$run_dir"
run_dir="$(realpath -- "$run_dir")"
controller_pid=''
stop_controller() {
    if [[ -n "$controller_pid" ]]; then
        kill -TERM "$controller_pid" 2>/dev/null || true
        wait "$controller_pid" 2>/dev/null || true
        controller_pid=''
    fi
}
trap stop_controller EXIT
trap 'exit 130' INT TERM
start_controller() {
    local database="$1" log="$2" model="$3"
    "$controller_python" -u controller.py --ofp-listen-host 127.0.0.1 \
        --ofp-tcp-listen-port "$controller_port" --telemetry-enabled \
        --telemetry-db "$database" --telemetry-model "$model" >"$log" 2>&1 &
    controller_pid=$!
    "$controller_python" - "$database" "$controller_port" <<'PY'
import pathlib, socket, sqlite3, sys, time
database, port = pathlib.Path(sys.argv[1]), int(sys.argv[2])
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True) as connection:
            assert connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            break
    except (OSError, sqlite3.Error, AssertionError):
        time.sleep(0.25)
else:
    raise SystemExit('Controller/database not ready; inspect controller log')
PY
}
for seed in 17 29 43; do
    database="$run_dir/session-$seed.sqlite"
    start_controller "$database" "$run_dir/controller-$seed.log" "$run_dir/model-not-trained.pkl"
    sudo /usr/bin/python3 collect_phase3.py --database "$database" \
        --output "$run_dir/traffic-$seed" --seed "$seed" --seconds "$session_seconds" \
        --controller-port "$controller_port" 2>&1 | tee "$run_dir/collection-$seed.log"
    stop_controller
done
"$controller_python" research_phase3.py "$run_dir/session-17.sqlite" \
    "$run_dir/session-29.sqlite" "$run_dir/session-43.sqlite" \
    --output "$run_dir/evaluation" >"$run_dir/research-stdout.json"
# A fourth unseen session tests the already frozen model, without retraining.
database="$run_dir/inference-71.sqlite"
start_controller "$database" "$run_dir/controller-inference.log" "$run_dir/evaluation/model.pkl"
sudo /usr/bin/python3 collect_phase3.py --database "$database" \
    --output "$run_dir/traffic-71" --seed 71 --seconds "$session_seconds" \
    --controller-port "$controller_port" --phase2-checks 2>&1 | tee "$run_dir/collection-71.log"
"$controller_python" - "$database" >"$run_dir/live-session.txt" <<'PY'
import sys
from phase3 import sessions
print(sessions(sys.argv[1])[0]['session_id'])
PY
"$controller_python" phase3.py evaluate "$database" --session "$(cat "$run_dir/live-session.txt")" \
    >"$run_dir/live-matured-evaluation.json"
"$controller_python" phase3.py export "$database" --table predictions \
    --output "$run_dir/live-predictions.jsonl"
"$controller_python" phase3.py export "$database" --table observations \
    --output "$run_dir/live-observations.jsonl"
"$controller_python" research_phase3.py "$database" --audit-model "$run_dir/evaluation/model.pkl" \
    --output "$run_dir/live-audit" >"$run_dir/live-audit-stdout.json"
stop_controller
echo "Experiment finished. Evidence: $run_dir"
echo 'Inspect report.json, traffic logs, plots, and live-matured-evaluation.json before drawing conclusions.'
