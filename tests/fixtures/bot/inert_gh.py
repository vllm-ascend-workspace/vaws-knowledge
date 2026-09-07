#!/usr/bin/env python3
"""Inert local ``gh`` recorder for the original comment-step shell.

Records argv to ``GH_CALLS_LOG``. Never opens a network connection. GET
``--jq`` applies the original first-marker-match semantics against
``GH_COMMENTS_JSON`` so the historical defects remain reproducible.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MARKER = "<!-- vaws-knowledge-review-bot:v1 -->"


def main(argv: list[str]) -> int:
    log_path = os.environ.get("GH_CALLS_LOG")
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")

    method = "GET"
    jq = None
    api_path = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-X" and i + 1 < len(argv):
            method = argv[i + 1]
            i += 2
            continue
        if arg == "--jq" and i + 1 < len(argv):
            jq = argv[i + 1]
            i += 2
            continue
        if arg in {"--paginate", "api"}:
            i += 1
            continue
        if arg == "-f" and i + 1 < len(argv):
            i += 2
            continue
        if not arg.startswith("-") and api_path is None:
            api_path = arg
            i += 1
            continue
        i += 1

    if method == "GET" and jq is not None:
        raw = os.environ.get("GH_COMMENTS_JSON", "[]")
        comments_path = os.environ.get("GH_COMMENTS_PATH")
        if comments_path:
            comments = json.loads(Path(comments_path).read_text(encoding="utf-8"))
        else:
            comments = json.loads(raw)
        matched = [
            item
            for item in comments
            if str(item.get("body", "")).startswith(MARKER)
        ]
        if matched:
            sys.stdout.write(str(matched[0]["id"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
