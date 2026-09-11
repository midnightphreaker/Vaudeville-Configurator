#!/usr/bin/env bash
# Regenerate every README screenshot headlessly (Xvfb + ImageMagick), without
# touching your real config/state and without putting a window on your desktop.
#
#   tools/make_screenshots.sh [output-dir]        (default: docs/screenshots)
#
# Needs: xorg-server-xvfb (Xvfb) and imagemagick (import/convert).
# Env:   GAME=/path/to/Vaudeville   DISP=:99   WAIT=5
#
# Shots (the six tabs the GUI has now):
#   home-off.png        the Home page: three setup cards, no setup confirmed yet,
#                       so Home is the only tab in the strip
#   tab-basic.png       Local Mode - Basic          (mode=off,    confirmed)
#   tab-advanced.png    Local Mode - Advanced       (mode=direct, confirmed)
#   tab-remote.png      Remote Mode - OpenAI API    (mode=shim,   confirmed)
#   tab-characters.png  Characters, Police Station group selected
#   tab-backup.png      Backup / restore
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
# optional: without it the Xvfb pointer stays at the screen centre and a hover
# tooltip can end up in the middle of a shot
command -v xdotool >/dev/null || echo "note: xdotool missing - a hover tooltip may appear in shots" >&2

TMP="$(mktemp -d)"
export XDG_CONFIG_HOME="$TMP/config" XDG_STATE_HOME="$TMP/state" XDG_DATA_HOME="$TMP/data"
mkdir -p "$XDG_CONFIG_HOME/vaudville-configurator" "$XDG_STATE_HOME" "$XDG_DATA_HOME"
CFG="$XDG_CONFIG_HOME/vaudville-configurator/config.json"

GAME="${GAME:-$(python3 vaudville_configurator.py --cli detect 2>/dev/null | head -1 | cut -f1)}"
[ -n "$GAME" ] || { echo "no Vaudeville install found; set GAME=/path/to/Vaudeville" >&2; exit 3; }
echo "### game dir: $GAME"
echo "### output:   $(cd "$OUT" && pwd)"

# tall enough that every tab fits without scrolling; the shots are trimmed
Xvfb "$DISP" -screen 0 1600x1500x24 -nolisten tcp >"$TMP/xvfb.log" 2>&1 &
XV=$!
trap 'kill $XV 2>/dev/null; wait $XV 2>/dev/null' EXIT
SOCK="/tmp/.X11-unix/X${DISP#:}"
for i in $(seq 1 40); do
  [ -S "$SOCK" ] && kill -0 $XV 2>/dev/null && break
  kill -0 $XV 2>/dev/null || break
  sleep 0.25
done
# A stale $SOCK from an earlier run makes the old "-e" test pass while Xvfb has
# already died, and every capture then silently produces a black image.
if ! kill -0 $XV 2>/dev/null; then
  echo "Xvfb $DISP did not start (display busy, or a stale $SOCK); set DISP=:96 or similar" >&2
  sed 's/^/    /' "$TMP/xvfb.log" >&2
  exit 3
fi
export DISPLAY="$DISP"

# write_cfg <mode> <mode_confirmed> <cast_group> <backend_url> <backend_model>
#           <direct_host> <direct_port>
# The Home page hides every tab except Home until a setup has been confirmed, so
# each shot says whether the setup was confirmed (false only for home-off.png).
write_cfg() {
  cat > "$CFG" <<JSON
{
  "game_dir": "$GAME",
  "mode": "$1",
  "mode_confirmed": $2,
  "cast_group": "$3",
  "shim_listen": "127.0.0.1",
  "shim_port": 13333,
  "shim_mode": "chat",
  "shim_backend_url": "$4",
  "backend_url": "$4",
  "backend_model": "$5",
  "direct_host": "$6",
  "direct_port": $7,
  "api_key": "",
  "save_api_key": false,
  "strip_think": true,
  "expose_server": false,
  "server_port": 13333,
  "primary_model": "",
  "deck_model": "",
  "agent_params": {},
  "llm_params": {}
}
JSON
}

# shot <name> <tab> <mode> <confirmed> <group> <geometry> [url] [model] [host] [port]
shot() {
  local name="$1" tab="$2" mode="$3" confirmed="$4" group="$5" geo="$6"
  local url="${7:-}" model="${8:-}" host="${9:-}" port="${10:-0}"
  local quit
  quit="$(awk -v w="$WAIT" 'BEGIN{printf "%.1f", w+4}')"
  write_cfg "$mode" "$confirmed" "$group" "$url" "$model" "$host" "$port"
  python3 vaudville_configurator.py --tab "$tab" --geometry "$geo" --quit-after "$quit" \
      >"$TMP/gui-$name.log" 2>&1 &
  local gui=$!
  sleep "$WAIT"
  # Xvfb parks the pointer at the screen centre, which lands on a widget and
  # opens its hover tooltip mid-shot.  Move it outside the window first; the
  # cursor sprite is not part of the framebuffer, so it never shows up.
  if command -v xdotool >/dev/null 2>&1; then
    xdotool mousemove 1590 1490 >/dev/null 2>&1 || true
    sleep 0.4
  fi
  import -window root -display "$DISP" "$TMP/raw-$name.png" 2>>"$TMP/gui-$name.log"
  "$MAGICK" "$TMP/raw-$name.png" -trim +repage "$OUT/$name.png"
  wait $gui 2>/dev/null
  if grep -qi 'traceback\|TclError' "$TMP/gui-$name.log"; then
    echo "  !! $name: the GUI logged an error — see $TMP/gui-$name.log" >&2
  fi
  printf '  %-22s %-10s tab=%-11s mode=%-7s confirmed=%-5s group=%s\n' \
      "$name.png" "$(identify -format '%wx%h' "$OUT/$name.png" 2>/dev/null)" \
      "$tab" "$mode" "$confirmed" "$group"
}

shot home-off       home        off     false all             1240x1280
shot tab-basic      basic       off     true  all             1200x1180
shot tab-advanced   advanced    direct  true  all             1200x1420 "" "" "127.0.0.1" 8080
shot tab-remote     remote      shim    true  all             1200x1300 "http://127.0.0.1:8000/v1" "llama-3.1-8b-instruct"
shot tab-characters characters  off     true  police_station  1240x1280
shot tab-backup     backup      off     true  all             1200x820

echo "### done: $(ls -1 "$OUT"/*.png | wc -l) screenshots in $OUT"
