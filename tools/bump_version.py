#!/usr/bin/env python3
"""Keep VERSION and APP_VERSION in vaudeville_configurator.py synchronised.

    tools/bump_version.py 1.4.0

Used by the release workflow (all platforms) and safe to run by hand.
"""
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[1]
v = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
if not re.fullmatch(r"\d+\.\d+\.\d+", v):
    sys.exit(f"usage: bump_version.py X.Y.Z (got {v!r})")

(root / "VERSION").write_text(v + "\n", encoding="utf-8")
p = root / "vaudeville_configurator.py"
t = p.read_text(encoding="utf-8")
new, n = re.subn(r'^APP_VERSION = ".*"', f'APP_VERSION = "{v}"', t, count=1, flags=re.M)
if n != 1:
    sys.exit("APP_VERSION line not found exactly once")
p.write_text(new, encoding="utf-8")
print(f"version -> {v} (VERSION + APP_VERSION)")
