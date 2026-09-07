#!/usr/bin/env python3
"""A minimal redaction gate, good enough to pass the gate vectors.

Not a proposal for tools/redact.py - the real ruleset is versioned as a
`redaction_profile` and has to cover far more than five shapes. This fixture
exists to show that the six redaction vectors are passable by an ordinary
implementation, and in particular that the clean control survives: the
hostname rule matches on a plausible TLD rather than on "a word with a dot in
it", because the corpus is full of things like torch.distributed and
0.0.0+example.

Prints `reject` or `accept` on stdout.
"""

import re
import sys

import yaml

PATTERNS = {
    "ipv4": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "user_path": re.compile(r"(?:/home/|/Users/)[^/\s]+"),
    "credential": re.compile(
        r"(?i)(?:bearer\s+\S+|(?:token|password|passwd|secret|api[_-]?key)\s*[=:]\s*\S+)"
    ),
    "hostname": re.compile(
        r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*"
        # No "example" or "test" in this list on purpose: the corpus is full of
        # synthetic version strings like 0.0.EXAMPLE and 2.5.1.example, and a
        # gate that treats those as hostnames refuses the clean control.
        r"\.(?:com|net|org|io|cn|internal|intranet|local|localdomain|invalid)\b"
    ),
}


def strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    elif isinstance(node, str):
        yield node


def main():
    document = yaml.safe_load(sys.stdin.read())
    findings = []
    for text in strings(document):
        for name, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                findings.append(f"{name}: {match.group(0)}")
    if findings:
        print("reject")
        for finding in findings:
            sys.stderr.write(finding + "\n")
        return 1
    print("accept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
