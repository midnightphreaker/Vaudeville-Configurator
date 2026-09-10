#!/usr/bin/env bash
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
"$BIN/llama-server" -m "$MODEL" --host 127.0.0.1 --port 8081 -c 4096 -ngl 0 --jinja \
    > logs/llama-server-direct.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
# wait for a real 200 on /health
for i in $(seq 1 240); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:8081/health 2>/dev/null)
  [ "$code" = "200" ] && break
  sleep 0.5
done
echo "### /health -> HTTP $code after $((i/2))s"
curl -s -m 5 http://127.0.0.1:8081/v1/models | head -c 300; echo
echo "################ direct: game libllamalib -> real llama-server :8081 (no shim)"
timeout 240 python3 -u "$TOOLS/poc_remote_llamalib.py" --host http://127.0.0.1 --port 8081 \
  --prompt "In one short sentence, who are you and where are we?" 2>&1 | sed 's/^/[A] /' | tail -14
