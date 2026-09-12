"""One optional GitHub reaction for a published experience."""

from __future__ import annotations

import argparse
import json

from vaws_knowledge.feedback import experience_feedback
from vaws_knowledge.server.layers import load_config


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--vote", required=True, choices=("+1", "-1"))
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    result = experience_feedback(load_config(path=args.config), args.ref, args.vote)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in {"ok", "disabled"} else 1
