#!/usr/bin/env bash
# Build the single-file executable for the CURRENT platform (Linux/macOS).
# Windows: use tools\build_package.cmd on a Windows machine (no cross-compile).
#
#   bash tools/build_package.sh            -> dist/vaudeville-configurator
#   NAME=myname bash tools/build_package.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"
NAME="${NAME:-vaudeville-configurator}"
VENV="${VENV:-.venv-build}"          # build-only venv; never touches system python

# Pure-python data modules the GUI depends on.  Declared explicitly so the
# bundle is correct however vaudeville_configurator.py imports them: top level,
# inside a "try: ... except ImportError:" fallback, or lazily inside a
# function.  The generated vaudeville-configurator.spec is NOT a sync peer
# (pyinstaller regenerates it on every build); the three entry points - this
# script, tools/build_package.cmd and the release workflow - are the source of
# truth and must carry the same list.
EXTRA_MODULES="${EXTRA_MODULES:-vaudeville_cast vaudeville_help}"

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

HIDDEN=()
for m in $EXTRA_MODULES; do
  HIDDEN+=(--hidden-import "$m")
done

# ${HIDDEN[@]+...} keeps "set -u" happy on bash 3.2 (macOS) if the list is empty.
"$VENV/bin/pyinstaller" --onefile --clean --name "$NAME" \
  ${HIDDEN[@]+"${HIDDEN[@]}"} vaudeville_configurator.py

# Prove the data modules really landed inside the bundle.  A module whose .py
# is absent from the tree is skipped: PyInstaller only warns about an
# unresolvable --hidden-import, and we do not want to fail a build for a file
# that legitimately is not part of this checkout.
listing="$("$VENV/bin/python" -m PyInstaller.utils.cliutils.archive_viewer \
             -l -r -b "dist/$NAME" 2>/dev/null || true)"
if [ -z "$listing" ]; then
  echo "### ERROR: archive_viewer gave no output; cannot verify bundle contents" >&2
  exit 1
else
  for m in $EXTRA_MODULES; do
    if [ ! -f "$m.py" ]; then
      echo "### note: $m.py not in this checkout; skipping its bundle assertion"
      continue
    fi
    if grep -qE "^[[:space:]]*$m\$" <<<"$listing"; then
      echo "### bundle contains $m"
    else
      echo "### ERROR: $m is missing from dist/$NAME" >&2
      exit 1
    fi
  done
fi

echo "### built: $ROOT/dist/$NAME  ($(stat -c %s "dist/$NAME" 2>/dev/null || stat -f %z "dist/$NAME") bytes)"
