#!/usr/bin/env python3
"""A second, independent implementation of the canonicalization.

Written from docs/federation.md rather than from conformance/reference.py, and
in a deliberately different style - translate-based ASCII lowercasing, an
explicit UTF-8 sort key, split-on-LF after CR normalization. It exists so the
test suite can show that the vectors are reproducible from the spec by someone
who did not write the reference, which is the only interesting property a test
fixture can have here.

If this file and conformance/reference.py ever disagree, one of them has
misread the spec.
"""

import hashlib
import json
import sys

import yaml

ASCII_WS = " \t\n\r\x0b\x0c"
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def to_lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def plain(text):
    text = to_lf(text)
    text = "\n".join(line.rstrip(ASCII_WS) for line in text.split("\n"))
    return text.strip(ASCII_WS)


def collapse_ascii_ws(text):
    out = []
    in_ws = False
    for char in text:
        if char in ASCII_WS:
            if not in_ws:
                out.append(" ")
            in_ws = True
        else:
            out.append(char)
            in_ws = False
    return "".join(out)


def fingerprints(items):
    cleaned = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = collapse_ascii_ws(item.translate(ASCII_LOWER).strip(ASCII_WS))
        if text:
            cleaned.add(text)
    return sorted(cleaned, key=lambda s: s.encode("utf-8"))


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
