#!/usr/bin/env python3
"""Stand-in for tools/redact.py --check in sync tests: accepts everything.

Exits non-zero only when --check is absent or a path does not exist, so a
miswired invocation fails instead of silently passing the gate.
"""
import pathlib
import sys

args = sys.argv[1:]
if not args or args[0] != "--check":
    print("redact stub: expected --check <paths...>", file=sys.stderr)
    sys.exit(2)
missing = [p for p in args[1:] if not pathlib.Path(p).exists()]
if missing:
    print("redact stub: missing paths " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"redact stub: {len(args) - 1} path(s) clean")
