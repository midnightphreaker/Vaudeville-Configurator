#!/usr/bin/env bash
# End-to-end verification: Vaudeville's own libllamalib -> shim -> mock OpenAI backend
# The shim under test is the SHIPPED one -- `vaudville_configurator.py --shim --config <json>`,
# the exact entrypoint _shim_argv() spawns -- not tools/llamalib_shim.py, which stays only as
# the standalone reference implementation. Headless protocol test: no GUI, no X, and an
# isolated XDG_* home so nothing is written to the real config/state or the game install.
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
fail() { echo "### FAIL: $*" >&2; exit 1; }
TOOLS=""
for d in "$HERE/../tools" "$HERE/tools" "$HERE"; do
  [ -f "$d/llamalib_shim.py" ] && TOOLS="$d" && break
done
[ -n "$TOOLS" ] || { echo "cannot locate tools/ (llamalib_shim.py) relative to $HERE" >&2; exit 3; }
CFG=""
for c in "$HERE/../vaudville_configurator.py" "$HERE/vaudville_configurator.py"; do
  [ -f "$c" ] && CFG="$c" && break
done
[ -n "$CFG" ] || { echo "cannot locate vaudville_configurator.py relative to $HERE" >&2; exit 3; }
# every port is overridable so two runs cannot collide
MOCK_PORT="${MOCK_PORT:-18080}"
SHIM_PORT="${SHIM_PORT:-13333}"
TEST_API_KEY="${TEST_API_KEY:-sk-test-123}"   # fixture; reaches the shim via VLM_API_KEY only
# BACKEND_URL lets you point the shim somewhere else (e.g. a dead port) to prove the
# assertions below really fail; the default is the mock this script starts.
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:$MOCK_PORT/v1}"
export PYTHONDONTWRITEBYTECODE=1
RUN_HOME="${RUN_HOME:-$(mktemp -d "${TMPDIR:-/tmp}/vlm-e2e.XXXXXX")}"   # throwaway XDG home
mkdir -p "$RUN_HOME"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$RUN_HOME/config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$RUN_HOME/data}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$RUN_HOME/state}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$RUN_HOME/cache}"
mkdir -p logs
python3 -u "$TOOLS/mock_openai_backend.py" "$MOCK_PORT" > logs/mock.log 2>&1 &
MOCK=$!
# shim config in the shape ShimProcess.write_config() writes: no api_key on disk, none in argv
SHIM_CFG="$RUN_HOME/shim-config.json"
cat > "$SHIM_CFG" <<EOF
{"listen": "127.0.0.1", "port": $SHIM_PORT, "mode": "${MODE:-chat}",
 "backend_url": "$BACKEND_URL", "backend_model": "mock-model",
 "strip_think": true, "verbose": true}
EOF
[ -s "$SHIM_CFG" ] || fail "could not write the shim config to $SHIM_CFG"
VLM_API_KEY="$TEST_API_KEY" python3 -u "$CFG" --shim --config "$SHIM_CFG" > logs/shim.log 2>&1 &
SHIM=$!
trap 'kill $MOCK ${SHIM:-} 2>/dev/null' EXIT
for i in $(seq 1 80); do
  curl -s -m 2 -X POST "http://127.0.0.1:$SHIM_PORT/health" >/dev/null 2>&1 && break
  kill -0 "$SHIM" 2>/dev/null || break       # shim died at startup: stop waiting
  sleep 0.25
done
health=$(curl -s -m 3 -X POST "http://127.0.0.1:$SHIM_PORT/health")
echo "### shim health: $health"
[ -n "$health" ] || { sed 's/^/[shim] /' logs/shim.log >&2
                      fail "shim did not answer /health on :$SHIM_PORT (log above)"; }
out=$(timeout 90 python3 -u "$TOOLS/poc_remote_llamalib.py" --port "$SHIM_PORT" 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/[poc] /'
echo "### ---- shim log ----"; sed 's/^/[shim] /' logs/shim.log
echo "### ---- mock log ----"; sed 's/^/[mock] /' logs/mock.log
# ---- assertions: a green run must really have produced text ---------------------
[ "$rc" -eq 0 ] || fail "poc exited rc=$rc"
final=$(printf '%s\n' "$out" | sed -n 's/.*FINAL TEXT: //p' | tail -1)
case "$final" in ''|"''"|'""') fail "empty FINAL TEXT (poc rc=$rc) - shim or backend did not answer" ;; esac
cbs=$(printf '%s\n' "$out" | sed -n 's/.*streamed callbacks: \([0-9]*\).*/\1/p' | tail -1)
{ [ -n "$cbs" ] && [ "$cbs" -gt 0 ]; } || fail "no streamed callbacks (got '${cbs:-none}')"
grep -q "Bearer $TEST_API_KEY" logs/mock.log || fail "mock backend never saw the key from VLM_API_KEY"
echo "### PASS: libllamalib -> shipped shim -> mock OpenAI (FINAL TEXT ${#final} chars, $cbs callbacks)"
