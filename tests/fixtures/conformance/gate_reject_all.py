#!/usr/bin/env python3
"""Rejects everything. Must fail the accept-control vectors.

The mirror image of gate_accept_all: it passes every rejection vector while
being useless, which is what the accept controls are in the kit to catch.
"""

import sys

sys.stdin.read()
print("reject")
raise SystemExit(1)
