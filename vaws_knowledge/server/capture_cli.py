"""CLI for Markdown capture and delete."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vaws_knowledge.server.capture import capture, delete
from vaws_knowledge.server.layers import load_config


def main(argv: list[str], *, kind: str = "knowledge") -> int:
    parser = argparse.ArgumentParser(description=f"Capture or delete a local {kind} candidate")
    parser.add_argument("--title", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--content-file", default="")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ref", help="revise an existing local candidate, preserving its path")
    target.add_argument("--public-relpath", help="fixed knowledge/entry.md or existing experience/case.md corpus path")
    parser.add_argument("--delete", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--backend", default="")
    args = parser.parse_args(argv)
    mapping = {"backend": args.backend} if args.backend else None
    config = load_config(mapping, path=args.config or None).for_kind(kind)
    if args.delete:
        print(json.dumps(delete(args.delete, config=config), ensure_ascii=False, indent=2))
        return 0
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    payload = capture(title=args.title, content=content, ref=args.ref, public_relpath=args.public_relpath,
                      config=config, index=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
