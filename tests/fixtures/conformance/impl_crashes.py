#!/usr/bin/env python3
"""Fails on every input, the way a missing dependency would."""

import sys

sys.stdin.read()
sys.stderr.write("canonical: cannot import the version module\n")
raise SystemExit(3)
