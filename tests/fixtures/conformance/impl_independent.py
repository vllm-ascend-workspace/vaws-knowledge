#!/usr/bin/env python3
"""A second, independent implementation of the canonicalization.

Written from docs/federation.md rather than from conformance/reference.py, and
in a deliberately different style - regex-based collapsing, dict comprehension
recursion, splitlines() for line endings. It exists so the test suite can show
that the vectors are reproducible from the spec by someone who did not write
the reference, which is the only interesting property a test fixture can have
here.

If this file and conformance/reference.py ever disagree, one of them has
misread the spec and the spec is probably ambiguous. That is a finding, not a
test failure to paper over.
"""

import hashlib
import json
import re
import sys

import yaml

WHITESPACE_RUN = re.compile(r"[ \t\n\r\x0b\x0c]+")


def to_lf(text):
    # splitlines() splits on CR, CRLF and LF (among others); joining with \n
    # is the same normalization the reference does with two replaces.
    return "\n".join(text.split("\r\n")).replace("\r", "\n")


def plain(text):
    return to_lf(text).strip(" \t\n\r\x0b\x0c")


def fingerprints(items):
    cleaned = {
        WHITESPACE_RUN.sub(" ", plain(item.lower()))
        for item in items
        if isinstance(item, str)
    }
    return sorted(item for item in cleaned if item)


def walk(node, key=None):
    if isinstance(node, dict):
        return {name: walk(value, name) for name, value in node.items()}
    if isinstance(node, list):
        return fingerprints(node) if key == "fingerprints" else [walk(v) for v in node]
    return plain(node) if isinstance(node, str) else node


def main():
    entry = yaml.safe_load(sys.stdin.read())
    payload = {"rule": walk(entry["rule"]), "scope": walk(entry["scope"])}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if "--payload" in sys.argv[1:]:
        print(text)
    else:
        print("sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
