#!/usr/bin/env python3
"""Stand-in for tools/validate.py in sync tests: accepts every path given.

sync/ shells out to the real tool and must not import it; this stub lets the
sync suite run before tools/ exists and keeps it independent of that tool's
behaviour. It exits non-zero only if a path does not exist, so that a test
passing the wrong path fails loudly instead of "passing" the gate.
"""
import pathlib
import sys

missing = [p for p in sys.argv[1:] if not pathlib.Path(p).exists()]
if missing:
    print("validate stub: missing paths " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"validate stub: {len(sys.argv) - 1} path(s) accepted")
