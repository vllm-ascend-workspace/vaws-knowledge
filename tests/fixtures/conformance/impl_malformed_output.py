#!/usr/bin/env python3
"""Prints something that is not a content_hash at all.

A conforming implementation prints `sha256:<64 hex>` and nothing else on
stdout. Chatty output ("computed hash: ...") is a real and common way for a
client to break a machine contract, so the runner has to reject it rather than
try to parse it out.
"""

import sys

sys.stdin.read()
print("computed hash successfully, see log for details")
