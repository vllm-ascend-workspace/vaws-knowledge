#!/usr/bin/env python3
"""Runner adapter: hash via capture's alias of ``vaws_knowledge.canonical``.

Calls builtin_content_hash / canonical_payload, which now delegate to the
single implementation in ``vaws_knowledge.canonical``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
from vaws_knowledge.server import capture  # noqa: E402


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
            sys.stdout.write(capture.canonical_payload(entry) + "\n")
        else:
            sys.stdout.write(capture.builtin_content_hash(entry) + "\n")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"impl_server: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
