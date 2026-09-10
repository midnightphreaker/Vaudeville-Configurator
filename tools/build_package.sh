#!/usr/bin/env bash
# Build the single-file executable for the CURRENT platform (Linux/macOS).
# Windows: use tools\build_package.cmd on a Windows machine (no cross-compile).
#
#   bash tools/build_package.sh            -> dist/vaudville-configurator
#   NAME=myname bash tools/build_package.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"
NAME="${NAME:-vaudville-configurator}"
VENV="${VENV:-.venv-build}"          # build-only venv; never touches system python
if [ ! -x "$VENV/bin/pyinstaller" ]; then
  if ! python3 -m venv "$VENV" >/dev/null 2>&1 \
     || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    # distro python without ensurepip (e.g. Debian CI images): venv + get-pip bootstrap
    rm -rf "$VENV"
    python3 -m venv --without-pip "$VENV"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$VENV/get-pip.py"
    "$VENV/bin/python" "$VENV/get-pip.py" --no-warn-script-location
  fi
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install pyinstaller
fi
"$VENV/bin/pyinstaller" --onefile --clean --name "$NAME" vaudville_configurator.py
echo "### built: $ROOT/dist/$NAME  ($(stat -c %s "dist/$NAME" 2>/dev/null || stat -f %z "dist/$NAME") bytes)"
