#!/usr/bin/env bash
# Real end-to-end test: Vaudeville's own libllamalib  ->  (a) a real llama.cpp server
#                                                     ->  (b) shim -> llama-server /v1
set -u
cd "$(dirname "$0")"
HERE="$(cd "$(dirname "$0")" && pwd)"
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
MODEL=/home/mp/Workspace/Vaudeville/Vaudeville_Data/StreamingAssets/Qwen3-0.6B-Q4_K_M.gguf
mkdir -p logs
"$BIN/llama-server" -m "$MODEL" --host 127.0.0.1 --port 8080 -c 4096 -ngl 0 \
    --jinja > logs/llama-server.log 2>&1 &
SRV=$!
trap 'kill $SRV $SHIM 2>/dev/null' EXIT
for i in $(seq 1 240); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:8080/health 2>/dev/null)
  [ "$code" = "200" ] && break; sleep 0.5
done
echo "### llama-server /health -> HTTP $code after ~$((i/2))s"
echo "### llama-server version: $(curl -s -m 3 http://127.0.0.1:8080/v1/models | head -c 200)"

echo; echo "################ TEST A: game lib -> real llama-server directly (no shim)"
timeout 180 python3 -u "$TOOLS/poc_remote_llamalib.py" --host http://127.0.0.1 --port 8080 \
  --prompt "In one short sentence, who are you?" 2>&1 | sed 's/^/[A] /' | tail -18

echo; echo "################ TEST B: game lib -> shim -> llama-server /v1/chat/completions"
python3 -u "$TOOLS/llamalib_shim.py" --listen 127.0.0.1 --port 13333 \
  --backend http://127.0.0.1:8080/v1 --model qwen3 --mode chat --verbose > logs/shim_real.log 2>&1 &
SHIM=$!
for i in $(seq 1 40); do curl -s -m 2 -X POST http://127.0.0.1:13333/health >/dev/null 2>&1 && break; sleep 0.25; done
timeout 180 python3 -u "$TOOLS/poc_remote_llamalib.py" --host http://127.0.0.1 --port 13333 \
  --prompt "In one short sentence, who are you?" 2>&1 | sed 's/^/[B] /' | tail -18
echo; echo "### shim log:"; tail -6 logs/shim_real.log | sed 's/^/[shim] /'
