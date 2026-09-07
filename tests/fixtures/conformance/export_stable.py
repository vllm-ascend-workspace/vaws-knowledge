#!/usr/bin/env python3
"""An exporter whose output depends only on its input. Must be idempotent."""

import sys

import yaml

document = yaml.safe_load(sys.stdin.read())
sys.stdout.write(
    yaml.safe_dump(document, sort_keys=True, allow_unicode=True, default_flow_style=False)
)
