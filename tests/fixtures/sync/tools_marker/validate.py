#!/usr/bin/env python3
"""Stand-in for tools/validate.py: accepts every existing path (see tools_pass)."""
import pathlib
import sys

missing = [p for p in sys.argv[1:] if not pathlib.Path(p).exists()]
if missing:
    print("validate stub: missing paths " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"validate stub: {len(sys.argv) - 1} path(s) accepted")
