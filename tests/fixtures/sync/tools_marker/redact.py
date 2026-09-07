#!/usr/bin/env python3
"""Stand-in for a *tightened* tools/redact.py --check.

Fails any file containing the literal marker QUARANTINE-ME, and reports the
line so the tests can prove that findings are withheld from the proposal
unless explicitly requested. The marker is synthetic; fixtures never contain
real addresses, hosts or paths.
"""
import pathlib
import sys

MARKER = "QUARANTINE-ME"

args = sys.argv[1:]
if not args or args[0] != "--check":
    print("redact stub: expected --check <paths...>", file=sys.stderr)
    sys.exit(2)
failed = False
for raw in args[1:]:
    path = pathlib.Path(raw)
    if not path.exists():
        print(f"redact stub: missing path {raw}", file=sys.stderr)
        failed = True
        continue
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if MARKER in line:
            print(f"{raw}:{lineno}: forbidden token found: {line.strip()}", file=sys.stderr)
            failed = True
sys.exit(1 if failed else 0)
