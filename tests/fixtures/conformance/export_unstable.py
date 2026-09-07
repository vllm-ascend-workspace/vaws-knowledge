#!/usr/bin/env python3
"""An exporter that stamps its own output. Must fail the idempotence vectors.

This is not a strawman: writing an export timestamp into the exported document
is a normal instinct, and it turns every re-export of an unchanged entry into
a revision proposal - a revision storm produced entirely by the exporter.
"""

import sys
import time

import yaml

document = yaml.safe_load(sys.stdin.read())
document["updated_at"] = "2026-09-07"
sys.stdout.write(f"# exported at {time.time_ns()}\n")
sys.stdout.write(
    yaml.safe_dump(document, sort_keys=True, allow_unicode=True, default_flow_style=False)
)
