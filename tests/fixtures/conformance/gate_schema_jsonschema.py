#!/usr/bin/env python3
"""A schema gate built straight on schemas/knowledge-v2.schema.json.

Requires jsonschema, and says so instead of raising a traceback when it is
missing. Prints `accept` or `reject`.
"""

import json
import pathlib
import sys

import yaml

SCHEMA = pathlib.Path(__file__).resolve().parents[3] / "schemas" / "knowledge-v2.schema.json"

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.stderr.write(
        "jsonschema is required by this fixture gate.\n"
        "Install it with: python3 -m pip install jsonschema\n"
    )
    raise SystemExit(2) from None


def main():
    if not SCHEMA.is_file():
        sys.stderr.write(f"schema not found: {SCHEMA}\n")
        return 2
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    document = yaml.safe_load(sys.stdin.read())
    errors = list(validator.iter_errors(document))
    if errors:
        print("reject")
        for error in errors[:5]:
            sys.stderr.write(f"{list(error.path)}: {error.message}\n")
        return 1
    print("accept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
