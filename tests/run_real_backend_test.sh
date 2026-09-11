#!/usr/bin/env bash
# Real end-to-end test: Vaudeville's own libllamalib  ->  (a) a real llama.cpp server
#                                                     ->  (b) shim -> llama-server /v1
# (b) drives the SHIPPED embedded shim (`vaudville_configurator.py --shim --config <json>`,
# the entrypoint _shim_argv() spawns); tools/llamalib_shim.py is only the standalone
# reference implementation. Headless: no GUI, no X, isolated XDG_* home.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"   # works from any cwd / any invocation style
cd "$HERE"
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
BIN="${LLAMA_BIN_DIR:-}"
if [ -z "$BIN" ]; then
  for c in "$HERE/../tools/llama.cpp" "$HERE/tools/llama.cpp" "$HERE/../Vaudeville/.codex/analysis/tools/llama.cpp-b10889/llama-b10889" "/home/mp/Workspace/Vaudeville/.codex/analysis/tools/llama.cpp-b10889/llama-b10889"; do
    [ -x "$c/llama-server" ] && BIN="$c" && break
  done
fi
if [ -z "$BIN" ]; then echo "llama-server not found; set LLAMA_BIN_DIR or put it in <configurator>/tools/llama.cpp" >&2; exit 3; fi
# test GGUF: $VLM_TEST_MODEL, else the first Vaudeville install we can see
MODEL="${VLM_TEST_MODEL:-}"
if [ -z "$MODEL" ]; then
  for c in "$HERE/../../Vaudeville/Vaudeville_Data/StreamingAssets/Qwen3-0.6B-Q4_K_M.gguf" \
           "$HOME/Workspace/Vaudeville/Vaudeville_Data/StreamingAssets/Qwen3-0.6B-Q4_K_M.gguf" \
           "$HOME/.local/share/Steam/steamapps/common/Vaudeville/Vaudeville_Data/StreamingAssets/Qwen3-0.6B-Q4_K_M.gguf" \
           "$HOME/.steam/steam/steamapps/common/Vaudeville/Vaudeville_Data/StreamingAssets/Qwen3-0.6B-Q4_K_M.gguf"; do
    [ -f "$c" ] && MODEL="$c" && break
  done
fi
if [ -z "$MODEL" ]; then echo "test GGUF not found; set VLM_TEST_MODEL=/path/to/model.gguf" >&2; exit 3; fi
# every port is overridable so two runs cannot collide
SRV_PORT="${SRV_PORT:-8080}"
SHIM_PORT="${SHIM_PORT:-13333}"
export PYTHONDONTWRITEBYTECODE=1
RUN_HOME="${RUN_HOME:-$(mktemp -d "${TMPDIR:-/tmp}/vlm-real.XXXXXX")}"   # throwaway XDG home
mkdir -p "$RUN_HOME"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$RUN_HOME/config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$RUN_HOME/data}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$RUN_HOME/state}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$RUN_HOME/cache}"
echo "### model: $MODEL"
mkdir -p logs
"$BIN/llama-server" -m "$MODEL" --host 127.0.0.1 --port "$SRV_PORT" -c 4096 -ngl 0 \
    --jinja > logs/llama-server.log 2>&1 &
SRV=$!
SHIM=""                                  # bound before the trap can fire
trap 'kill $SRV ${SHIM:-} 2>/dev/null' EXIT
for i in $(seq 1 240); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$SRV_PORT/health" 2>/dev/null)
  [ "$code" = "200" ] && break
  kill -0 "$SRV" 2>/dev/null || break        # server died (bad port, bad model): stop waiting
  sleep 0.5
done
echo "### llama-server /health -> HTTP $code after ~$((i/2))s"
[ "$code" = "200" ] || fail "llama-server never answered /health on :$SRV_PORT (logs/llama-server.log)"
echo "### llama-server version: $(curl -s -m 3 "http://127.0.0.1:$SRV_PORT/v1/models" | head -c 200)"

# a green run must really have produced text: $1=tag $2=poc rc $3=captured output
check_run() {
  local tag="$1" rc="$2" out="$3" final cbs
  [ "$rc" -eq 0 ] || fail "[$tag] poc exited rc=$rc"
  final=$(printf '%s\n' "$out" | sed -n 's/.*FINAL TEXT: //p' | tail -1)
  case "$final" in ''|"''"|'""') fail "[$tag] empty FINAL TEXT (poc rc=$rc)" ;; esac
  cbs=$(printf '%s\n' "$out" | sed -n 's/.*streamed callbacks: \([0-9]*\).*/\1/p' | tail -1)
  { [ -n "$cbs" ] && [ "$cbs" -gt 0 ]; } || fail "[$tag] no streamed callbacks (got '${cbs:-none}')"
  echo "### PASS [$tag]: FINAL TEXT ${#final} chars, $cbs callbacks"
}

echo; echo "################ TEST A: game lib -> real llama-server directly (no shim)"
outA=$(timeout 180 python3 -u "$TOOLS/poc_remote_llamalib.py" --host http://127.0.0.1 --port "$SRV_PORT" \
  --prompt "In one short sentence, who are you?" 2>&1); rcA=$?
printf '%s\n' "$outA" | sed 's/^/[A] /' | tail -18
check_run A "$rcA" "$outA"

echo; echo "################ TEST B: game lib -> shim -> llama-server /v1/chat/completions"
SHIM_CFG="$RUN_HOME/shim-config.json"
cat > "$SHIM_CFG" <<EOF
{"listen": "127.0.0.1", "port": $SHIM_PORT, "mode": "chat",
 "backend_url": "http://127.0.0.1:$SRV_PORT/v1", "backend_model": "qwen3",
 "strip_think": true, "verbose": true}
EOF
[ -s "$SHIM_CFG" ] || fail "could not write the shim config to $SHIM_CFG"
python3 -u "$CFG" --shim --config "$SHIM_CFG" > logs/shim_real.log 2>&1 &
SHIM=$!
for i in $(seq 1 80); do
  curl -s -m 2 -X POST "http://127.0.0.1:$SHIM_PORT/health" >/dev/null 2>&1 && break
  kill -0 "$SHIM" 2>/dev/null || break       # shim died at startup: stop waiting
  sleep 0.25
done
health=$(curl -s -m 3 -X POST "http://127.0.0.1:$SHIM_PORT/health" 2>/dev/null)
echo "### shim health: $health"
[ -n "$health" ] || { sed 's/^/[shim] /' logs/shim_real.log >&2
                      fail "shim did not answer /health on :$SHIM_PORT (log above)"; }
outB=$(timeout 180 python3 -u "$TOOLS/poc_remote_llamalib.py" --host http://127.0.0.1 --port "$SHIM_PORT" \
  --prompt "In one short sentence, who are you?" 2>&1); rcB=$?
printf '%s\n' "$outB" | sed 's/^/[B] /' | tail -18
echo; echo "### shim log:"; tail -6 logs/shim_real.log | sed 's/^/[shim] /'
check_run B "$rcB" "$outB"
# B is the whole point of the A/B pair: reasoning must not survive the shim
printf '%s\n' "$outB" | grep -q "<think>" && fail "[B] <think> leaked through the shim"
printf '%s\n' "$outA" | grep -q "<think>" \
  || echo "### note: [A] emitted no <think> this run, so the strip check above was vacuous"
echo "### PASS: A/B complete (raw vs shipped shim)"
