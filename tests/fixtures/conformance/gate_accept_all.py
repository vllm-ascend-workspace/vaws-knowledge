#!/usr/bin/env python3
"""Accepts everything. Must fail every rejection vector.

A gate that accepts everything is the failure mode a corpus never notices from
the inside, which is why the kit ships rejection vectors at all.
"""

import sys

sys.stdin.read()
print("accept")
