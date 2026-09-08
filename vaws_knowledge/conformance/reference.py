"""Readable reference rendering of the canonicalization in docs/federation.md.

`docs/federation.md` is the normative specification. This file is a
specification aid: the shortest readable code that renders those five steps,
kept deliberately dumb so that a reader can check it against the prose line by
line. It is not the production path, it is not optimised, and it must never be
imported by `tools/`, `server/`, `sync/` or `bot/` — an implementation that
calls this file proves nothing when it runs the conformance kit against
itself.

Its only jobs are:

  * produce the expected values in `conformance/vectors/`, and
  * give `conformance/selfcheck.py` something to check those vectors against.

Where the prose is ambiguous, this file makes the narrowest choice it can and
says so in a comment. Every such comment is also listed under "Where the spec
is ambiguous" in `conformance/README.md`, because a choice made here silently
would be exactly the divergence this kit exists to catch.

CLI contract (the same contract `conformance/runner.py` expects of any
implementation):

    printf '...entry yaml...' | python3 -m vaws_knowledge.conformance.reference
    -> sha256:<64 hex chars>

    python3 -m vaws_knowledge.conformance.reference --payload < entry.yaml
    -> the canonical JSON payload string, no trailing newline

Input shapes are selected with --format (default: a single entry as YAML).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

# --- step 1: only scope and the entry's body participate --------------------
#
# An entry has exactly one body: `rule` for a failure rule, `measurement` for
# a measured or vendor-declared quantity. The payload is `scope` plus that
# body, keyed by the body's own name. Two consequences worth stating, because
# both are load-bearing:
#
#   * a rule entry produces the identical payload it always did, so no
#     recorded content_hash moved when the second variant was added;
#   * an entry with no body, or with both, is not canonicalizable. It is a
#     schema violation (the schema's entry oneOf), and step 0 is validate
#     first, so refusing here rather than guessing is the narrow choice.
BODY_KEYS = ("rule", "measurement")

#: Kept for readers of the older name: the payload keys for a rule entry.
PAYLOAD_KEYS = ("rule", "scope")

# Whitespace for steps 2 and 3 is exactly these six ASCII characters.
# docs/federation.md names them: U+0009, U+000A, U+000B, U+000C, U+000D,
# U+0020. A non-breaking space or an ideographic space is content.
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def normalize_line_endings(text: str) -> str:
    """Step 3, first half: CRLF and lone CR become LF."""
    # Lone CR is a line ending. Vector `line-endings-lone-cr` pins that.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_string(text: str) -> str:
    """Step 3: LF endings, per-line trailing ASCII whitespace, then outer ASCII strip."""
    text = normalize_line_endings(text)
    text = "\n".join(line.rstrip(ASCII_WHITESPACE) for line in text.split("\n"))
    return text.strip(ASCII_WHITESPACE)


def collapse_whitespace(text: str) -> str:
    """Collapse every run of ASCII whitespace to a single U+0020 (step 2 only)."""
    out: list[str] = []
    previous_was_space = False
    for char in text:
        if char in ASCII_WHITESPACE:
            if not previous_was_space:
                out.append(" ")
            previous_was_space = True
        else:
            out.append(char)
            previous_was_space = False
    return "".join(out)


def ascii_lower(text: str) -> str:
    """Step 2: A–Z → a–z only. İ, É, Σ and so on are left unchanged."""
    return text.translate(ASCII_LOWER)


def normalize_fingerprints(items: list) -> list:
    """Step 2: ASCII-lower, ASCII-strip, collapse runs, drop empties, dedup, byte-sort."""
    normalized = []
    for item in items:
        if not isinstance(item, str):
            # Non-strings are schema violations, not canonicalization
            # questions. Pass them through untouched so a hash mismatch is not
            # mistaken for a normalization bug. Production CLIs reject them
            # before publishing a hash.
            normalized.append(item)
            continue
        text = collapse_whitespace(ascii_lower(item).strip(ASCII_WHITESPACE))
        if text:
            normalized.append(text)
    return sorted(set(normalized), key=lambda s: s.encode("utf-8"))


def normalize_value(value, *, in_fingerprints: bool = False):
    """Recursively normalize one payload node."""
    if isinstance(value, dict):
        # Keys are schema-fixed identifiers and are never normalized: the
        # schema's additionalProperties:false means an odd key is rejected
        # rather than cleaned up. Do not stringify a non-string key.
        return {
            key: normalize_value(sub, in_fingerprints=(key == "fingerprints"))
            for key, sub in value.items()
        }
    if isinstance(value, list):
        if in_fingerprints:
            return normalize_fingerprints(value)
        return [normalize_value(item) for item in value]
    if isinstance(value, str):
        return normalize_string(value)
    # null / bool pass through. A numeric bound is a schema problem: production
    # hash CLIs reject it rather than stringifying. This file still passes it
    # through so a vector author can see the JSON number, which is not a
    # published hash.
    return value


def body_key(entry: dict) -> str:
    """Name of the entry's single body key. Raises if there is not exactly one."""
    present = [key for key in BODY_KEYS if key in entry]
    if len(present) == 1:
        return present[0]
    if present:
        raise ValueError(f"entry declares more than one body: {present}")
    raise ValueError(f"entry declares none of the body keys {list(BODY_KEYS)}")


def canonical_payload(entry: dict) -> dict:
    """Steps 1-3: the normalized {<body>: ..., "scope": ...} payload."""
    body = body_key(entry)
    if "scope" not in entry:
        raise ValueError("entry is missing required payload key: 'scope'")
    # Absent optional keys stay absent. Nothing is defaulted in: an entry with
    # no `avoidance` must not hash like an entry with `avoidance: ""`.
    return {key: normalize_value(entry[key]) for key in (body, "scope")}


def canonical_json(entry: dict) -> str:
    """Step 4: the exact byte string that gets hashed."""
    return json.dumps(
        canonical_payload(entry),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def content_hash(entry: dict) -> str:
    """Step 5."""
    digest = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# --- CLI -------------------------------------------------------------------

FORMATS = ("entry-yaml", "entry-json", "document-yaml", "document-json")


def _require_yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        sys.stderr.write(
            "PyYAML is required to read YAML input.\n"
            "Install it with: python3 -m pip install PyYAML\n"
            "Or pass --format entry-json and feed JSON on stdin.\n"
        )
        raise SystemExit(2) from None
    return yaml


def load_entry(text: str, fmt: str, entry_index: int = 0) -> dict:
    """Parse stdin into a single entry mapping."""
    if fmt.endswith("-yaml"):
        data = _require_yaml().safe_load(text)
    else:
        data = json.loads(text)
    if data is None:
        raise ValueError("no input")
    if fmt.startswith("document-"):
        entries = data.get("entries") if isinstance(data, dict) else None
        if not entries:
            raise ValueError("document has no entries")
        return entries[entry_index]
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reference canonicalization for vaws-knowledge content_hash. "
            "Reads one entry on stdin, prints its content_hash on stdout."
        )
    )
    parser.add_argument("--format", choices=FORMATS, default="entry-yaml")
    parser.add_argument(
        "--entry-index",
        type=int,
        default=0,
        help="which entry to read when --format is a document form",
    )
    parser.add_argument(
        "--payload",
        action="store_true",
        help="print the canonical JSON payload instead of the hash",
    )
    args = parser.parse_args(argv)

    try:
        entry = load_entry(sys.stdin.read(), args.format, args.entry_index)
        out = canonical_json(entry) if args.payload else content_hash(entry)
    except SystemExit:
        raise
    except Exception as exc:  # a bad input is a message, not a traceback
        sys.stderr.write(f"reference: cannot canonicalize input: {exc}\n")
        return 2
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
