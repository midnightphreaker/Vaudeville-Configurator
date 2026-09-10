#!/usr/bin/env bash
# End-to-end verification: Vaudeville's own libllamalib -> shim -> mock OpenAI backend
set -u
cd "$(dirname "$0")"
HERE="$(pwd)"
TOOLS=""
for d in "$HERE/../tools" "$HERE/tools" "$HERE"; do
  [ -f "$d/llamalib_shim.py" ] && TOOLS="$d" && break
done
[ -n "$TOOLS" ] || { echo "cannot locate tools/ (llamalib_shim.py) relative to $HERE" >&2; exit 3; }
mkdir -p logs
python3 -u "$TOOLS/mock_openai_backend.py" 18080 > logs/mock.log 2>&1 &
MOCK=$!
python3 -u "$TOOLS/llamalib_shim.py" --listen 127.0.0.1 --port 13333 \
  --backend http://127.0.0.1:18080/v1 --model mock-model --api-key sk-test-123 \
  --mode "${MODE:-chat}" --verbose > logs/shim.log 2>&1 &
SHIM=$!
trap 'kill $MOCK $SHIM 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -s -m 2 -X POST http://127.0.0.1:13333/health >/dev/null && break; sleep 0.25; done
echo "### shim health: $(curl -s -m 3 -X POST http://127.0.0.1:13333/health)"
timeout 90 python3 -u "$TOOLS/poc_remote_llamalib.py" --port 13333 2>&1 | sed 's/^/[poc] /'
echo "### ---- shim log ----"; sed 's/^/[shim] /' logs/shim.log
echo "### ---- mock log ----"; sed 's/^/[mock] /' logs/mock.log
