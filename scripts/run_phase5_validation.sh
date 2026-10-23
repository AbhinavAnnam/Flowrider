#!/usr/bin/env bash
# Run from a normal terminal. Authentication is performed by sudo itself.
set -euo pipefail
cd "$(dirname "$0")/.."
phase5_python="${FLOWRIDER_PYTHON:-$HOME/.pyenv/versions/3.10.16/bin/python}"
phase5_model="${1:-data/research-20261015T180515Z/evaluation/model.pkl}"
phase5_port="${FLOWRIDER_OF_PORT:-16653}"
[[ -f "$phase5_model" ]] || { echo "Model missing: $phase5_model" >&2; exit 1; }
command -v iperf3 >/dev/null || { echo 'iperf3 is required.' >&2; exit 1; }
"$phase5_python" - "$phase5_port" <<'PY'
import socket,sys
with socket.socket() as s:
    s.bind(('127.0.0.1',int(sys.argv[1])))
PY
sudo -v
phase5_output="$PWD/data/phase5-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$phase5_output"
phase5_controller=''
phase5_validation=''
phase5_logger=''
cleanup() {
    phase5_status=$?
    trap '' INT TERM
    # sudo forwards TERM to the Python runner. Wait for its bounded child
    # cleanup and run_network's finally/net.stop() before stopping the controller.
    if [[ -n "$phase5_validation" ]]; then
        kill -TERM "$phase5_validation" 2>/dev/null || true
        wait "$phase5_validation" || true
    fi
    if [[ -n "$phase5_logger" ]]; then wait "$phase5_logger" || true; fi
    # Preserve the final snapshot on failed runs too. Timeout diagnostics contain
    # the earlier snapshot from before the lab was torn down.
    "$phase5_python" routing_state.py "$phase5_output/telemetry.sqlite" \
        >"$phase5_output/final-routing-state.json" 2>"$phase5_output/final-routing-state.stderr.log" || true
    if [[ -n "$phase5_controller" ]]; then
        if kill -0 "$phase5_controller" 2>/dev/null; then
            kill -TERM "$phase5_controller" 2>/dev/null || true
            for phase5_attempt in {1..50}; do
                kill -0 "$phase5_controller" 2>/dev/null || break
                sleep .2
            done
            if kill -0 "$phase5_controller" 2>/dev/null; then kill -KILL "$phase5_controller" 2>/dev/null || true; fi
        fi
        wait "$phase5_controller" || true
    fi
    if (( phase5_status != 0 )); then
        printf 'Live validation failed (exit %s). Evidence: %s\n' "$phase5_status" "$phase5_output" >&2
    fi
    return "$phase5_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$phase5_python" -u controller.py --ofp-listen-host 127.0.0.1 \
    --ofp-tcp-listen-port "$phase5_port" --telemetry-enabled \
    --telemetry-db "$phase5_output/telemetry.sqlite" --telemetry-model "$phase5_model" \
    --predictive-routing >"$phase5_output/controller.log" 2>&1 &
phase5_controller=$!
"$phase5_python" - "$phase5_port" <<'PY'
import socket,sys,time
end=time.monotonic()+12
while time.monotonic()<end:
    try:
        with socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=.3):break
    except OSError:time.sleep(.1)
else:raise SystemExit('Controller did not start; inspect controller.log')
PY
printf 'Phase 5 evidence: %s\n' "$phase5_output"
# Track sudo itself so EXIT/INT/TERM can reach the privileged runner. A
# foreground pipeline would leave no PID to forward a signal to.
exec {phase5_log_fd}> >(tee "$phase5_output/mininet.log")
phase5_logger=$!
sudo /usr/bin/python3 tests/integration_phase5.py --controller-port "$phase5_port" \
    --database "$phase5_output/telemetry.sqlite" --output "$phase5_output" \
    >&"$phase5_log_fd" 2>&1 &
phase5_validation=$!
exec {phase5_log_fd}>&-
phase5_result=0
wait "$phase5_validation" || phase5_result=$?
phase5_validation=''
wait "$phase5_logger" || { if (( phase5_result == 0 )); then phase5_result=1; fi; }
phase5_logger=''
if (( phase5_result != 0 )); then exit "$phase5_result"; fi
printf 'Live acceptance passed. Evidence: %s/acceptance.json\n' "$phase5_output"
