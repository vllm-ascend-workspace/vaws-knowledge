#!/usr/bin/env python3
"""Runner adapter: hash via the real sync._common fallback.

Does not import conformance/reference.py or tools.canonical.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sync import _common  # noqa: E402
from sync._common import SyncError  # noqa: E402


def _load(text: str):
    text = text.strip()
    if not text:
        raise ValueError("no input")
    if text[:1] in "{[":
        return json.loads(text)
    import yaml  # noqa: PLC0415

    return yaml.safe_load(text)


def main() -> int:
    try:
        entry = _load(sys.stdin.read())
        if "--payload" in sys.argv[1:]:
            sys.stdout.write(_common.canonical_json(entry) + "\n")
        else:
            sys.stdout.write(_common.content_hash(entry) + "\n")
    except (SyncError, ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"impl_sync: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
