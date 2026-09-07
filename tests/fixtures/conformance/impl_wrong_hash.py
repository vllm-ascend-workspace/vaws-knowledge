#!/usr/bin/env python3
"""Prints a well-formed hash that is always wrong. Every vector must fail."""

import sys

sys.stdin.read()
print("sha256:" + "f" * 64)
