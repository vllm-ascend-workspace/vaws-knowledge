#!/usr/bin/env python3
"""Runner adapter: hash via the real tools.canonical API in a subprocess.

Does not import conformance/reference.py. Reads one entry as YAML or JSON on
stdin and prints sha256:<hex> or the canonical JSON payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
from vaws_knowledge import canonical  # noqa: E402
from vaws_knowledge._common import ToolError  # noqa: E402


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
            sys.stdout.write(canonical.canonical_json(entry) + "\n")
        else:
            sys.stdout.write(canonical.content_hash(entry) + "\n")
    except (ToolError, ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"impl_tools: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
