#!/usr/bin/env python3
"""A near-miss implementation: correct except that it skips step 2.

Fingerprints are passed through as authored - no lowercasing, no collapsing, no
dedup, no sort. Entries whose fingerprints happen to be normalized already
still hash correctly, which is exactly why this class of bug survives in real
implementations until a vector like fingerprints-normalization exists.

Used by the test suite to show the runner reports per-vector rather than
pass/fail for the whole run.
"""

import hashlib
import json
import sys

import yaml


def plain(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if isinstance(value, dict):
        return {key: plain(sub) for key, sub in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def canonical(entry):
    rule = dict(entry["rule"])
    fingerprints = rule.pop("fingerprints", None)
    payload = {"rule": plain(rule), "scope": plain(entry["scope"])}
    if fingerprints is not None:
        payload["rule"]["fingerprints"] = list(fingerprints)  # step 2 skipped
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def main():
    entry = yaml.safe_load(sys.stdin.read())
    text = canonical(entry)
    if "--payload" in sys.argv[1:]:
        print(text)
    else:
        print("sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
