#!/usr/bin/env python3
"""A near-miss implementation: correct except that it skips step 2.

Fingerprints are passed through as authored - no ASCII lowercasing, no
collapsing, no dedup, no sort. Step 3 (LF endings, per-line trailing ASCII
whitespace, outer ASCII strip, no Unicode strip) is applied to every other
string. Entries whose fingerprints happen to be normalized already still hash
correctly, which is exactly why this class of bug survives in real
implementations until a vector like fingerprints-normalization exists.

Used by the test suite to show the runner reports per-vector rather than
pass/fail for the whole run.
"""

import hashlib
import json
import sys

import yaml

ASCII_WS = " \t\n\r\x0b\x0c"


def plain(value):
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(line.rstrip(ASCII_WS) for line in text.split("\n"))
        return text.strip(ASCII_WS)
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
