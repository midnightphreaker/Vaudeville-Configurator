#!/usr/bin/env bash
# Regenerate every README screenshot headlessly (Xvfb + ImageMagick), without
# touching your real config/state and without putting a window on your desktop.
#
#   tools/make_screenshots.sh [output-dir]        (default: docs/screenshots)
#
# Needs: xorg-server-xvfb (Xvfb) and imagemagick (import/convert).
# Env:   GAME=/path/to/Vaudeville   DISP=:99   WAIT=5
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"
OUT="${1:-docs/screenshots}"
DISP="${DISP:-:99}"
WAIT="${WAIT:-5}"
mkdir -p "$OUT"

MAGICK="$(command -v magick || echo convert)"
for c in Xvfb import "$MAGICK"; do
  command -v "$c" >/dev/null || { echo "missing $c (Arch: sudo pacman -S xorg-server-xvfb imagemagick)" >&2; exit 3; }
done

TMP="$(mktemp -d)"
export XDG_CONFIG_HOME="$TMP/config" XDG_STATE_HOME="$TMP/state" XDG_DATA_HOME="$TMP/data"
mkdir -p "$XDG_CONFIG_HOME/vaudville-configurator" "$XDG_STATE_HOME" "$XDG_DATA_HOME"
CFG="$XDG_CONFIG_HOME/vaudville-configurator/config.json"

GAME="${GAME:-$(python3 vaudville_configurator.py --cli detect 2>/dev/null | head -1 | cut -f1)}"
[ -n "$GAME" ] || { echo "no Vaudeville install found; set GAME=/path/to/Vaudeville" >&2; exit 3; }
echo "### game dir: $GAME"
echo "### output:   $ROOT/$OUT"

Xvfb "$DISP" -screen 0 1600x1200x24 -nolisten tcp >"$TMP/xvfb.log" 2>&1 &
XV=$!
trap 'kill $XV 2>/dev/null; wait $XV 2>/dev/null' EXIT
for i in $(seq 1 40); do [ -e "/tmp/.X11-unix/X${DISP#:}" ] && break; sleep 0.25; done
export DISPLAY="$DISP"

# shot <name> <tab> <mode> <backend_url> <backend_model> <geometry>
shot() {
  local name="$1" tab="$2" mode="$3" url="$4" model="$5" geo="${6:-1200x860}"
  cat > "$CFG" <<JSON
{
  "game_dir": "$GAME",
  "mode": "$mode",
  "shim_listen": "127.0.0.1",
  "shim_port": 13333,
  "backend_url": "$url",
  "backend_model": "$model",
  "api_key": "",
  "save_api_key": false,
  "strip_think": true,
  "shim_mode": "chat"
}
JSON
  python3 vaudville_configurator.py --tab "$tab" --geometry "$geo" --quit-after $((WAIT + 4)) \
      >"$TMP/gui-$name.log" 2>&1 &
  local gui=$!
  sleep "$WAIT"
  import -window root -display "$DISP" "$TMP/raw-$name.png" 2>>"$TMP/gui-$name.log"
  "$MAGICK" "$TMP/raw-$name.png" -trim +repage "$OUT/$name.png"
  wait $gui 2>/dev/null
  printf '  %-34s %s  (%s)\n' "$name.png" "$(identify -format '%wx%h' "$OUT/$name.png" 2>/dev/null)" "tab=$tab mode=$mode"
}

shot tab-game              game       off    ""                                  ""                        1200x900
shot tab-models            models     off    ""                                  ""                        1200x900
shot tab-remote-basic      remote     off    ""                                  ""                        1200x1060
shot tab-remote-advanced   remote     direct "http://127.0.0.1:8080"             "vaudeville-local"        1200x1060
shot tab-remote-openai     remote     shim   "http://127.0.0.1:8000/v1"          "llama-3.1-8b-instruct"   1200x1060
shot tab-parameters        parameters shim   "http://127.0.0.1:8000/v1"          "llama-3.1-8b-instruct"   1280x1000
shot tab-backup            backup     off    ""                                  ""                        1200x900

echo "### done: $(ls -1 "$OUT"/*.png | wc -l) screenshots"
