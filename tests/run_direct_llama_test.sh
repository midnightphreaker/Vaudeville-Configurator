#!/usr/bin/env bash
# Baseline control: Vaudeville's own libllamalib -> a real llama-server, no shim in the path.
# A <think> block in the reply is expected here; run_real_backend_test.sh is the one that
# proves the shipped shim strips it. Headless: no GUI, no X.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"   # works from any cwd / any invocation style
cd "$HERE"
fail() { echo "### FAIL: $*" >&2; exit 1; }
TOOLS=""
for d in "$HERE/../tools" "$HERE/tools" "$HERE"; do
  [ -f "$d/llamalib_shim.py" ] && TOOLS="$d" && break
done
[ -n "$TOOLS" ] || { echo "cannot locate tools/ (llamalib_shim.py) relative to $HERE" >&2; exit 3; }
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
# port is overridable so two runs cannot collide
SRV_PORT="${SRV_PORT:-8081}"
echo "### model: $MODEL"
mkdir -p logs
"$BIN/llama-server" -m "$MODEL" --host 127.0.0.1 --port "$SRV_PORT" -c 4096 -ngl 0 --jinja \
    > logs/llama-server-direct.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
# wait for a real 200 on /health
for i in $(seq 1 240); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$SRV_PORT/health" 2>/dev/null)
  [ "$code" = "200" ] && break
  kill -0 "$SRV" 2>/dev/null || break        # server died (bad port, bad model): stop waiting
  sleep 0.5
done
echo "### /health -> HTTP $code after $((i/2))s"
[ "$code" = "200" ] || fail "llama-server never answered /health on :$SRV_PORT (logs/llama-server-direct.log)"
curl -s -m 5 "http://127.0.0.1:$SRV_PORT/v1/models" | head -c 300; echo
echo "################ direct: game libllamalib -> real llama-server :$SRV_PORT (no shim)"
out=$(timeout 240 python3 -u "$TOOLS/poc_remote_llamalib.py" --host http://127.0.0.1 --port "$SRV_PORT" \
  --prompt "In one short sentence, who are you and where are we?" 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/[A] /' | tail -14
# ---- assertions: a green run must really have produced text ---------------------
[ "$rc" -eq 0 ] || fail "poc exited rc=$rc"
final=$(printf '%s\n' "$out" | sed -n 's/.*FINAL TEXT: //p' | tail -1)
case "$final" in ''|"''"|'""') fail "empty FINAL TEXT (poc rc=$rc) - llama-server did not answer" ;; esac
cbs=$(printf '%s\n' "$out" | sed -n 's/.*streamed callbacks: \([0-9]*\).*/\1/p' | tail -1)
{ [ -n "$cbs" ] && [ "$cbs" -gt 0 ]; } || fail "no streamed callbacks (got '${cbs:-none}')"
echo "### PASS: libllamalib -> llama-server :$SRV_PORT direct (FINAL TEXT ${#final} chars, $cbs callbacks)"
