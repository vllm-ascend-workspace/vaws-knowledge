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

# The documentation ranges are exempt per CONTRIBUTING.md. A gate that refuses
# them refuses the fixtures that prove it works, which is why the rule was
# resolved in the tool's favour rather than the prose's. Note that reserved is
# not the same as exempt: RFC 2544 benchmarking space (198.18.0.0/15) is
# reserved and still refused, because it does turn up in real internal networks.
ALLOWED = (
    re.compile(r"\b192\.0\.2\.\d{1,3}\b"),  # RFC 5737 TEST-NET-1
    re.compile(r"\b198\.51\.100\.\d{1,3}\b"),  # RFC 5737 TEST-NET-2
    re.compile(r"\b203\.0\.113\.\d{1,3}\b"),  # RFC 5737 TEST-NET-3
    re.compile(r"\b127\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),  # loopback
)

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
                found = match.group(0)
                if name == "ipv4" and any(a.fullmatch(found) for a in ALLOWED):
                    continue
                findings.append(f"{name}: {found}")
    if findings:
        print("reject")
        for finding in findings:
            sys.stderr.write(finding + "\n")
        return 1
    print("accept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
