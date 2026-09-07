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

    printf '...entry yaml...' | python3 conformance/reference.py
    -> sha256:<64 hex chars>

    python3 conformance/reference.py --payload < entry.yaml
    -> the canonical JSON payload string, no trailing newline

Input shapes are selected with --format (default: a single entry as YAML).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

# --- step 1: only scope and rule participate -------------------------------

PAYLOAD_KEYS = ("rule", "scope")

# The whitespace class used for stripping and for collapsing runs.
#
# AMBIGUITY: docs/federation.md says "whitespace" without defining it. Python's
# `\s` and `str.strip()` are Unicode-aware (they eat NBSP, ideographic space,
# U+2028 ...); a Go/Rust/JS implementation written from the same prose is very
# likely to be ASCII-only. This file takes the ASCII-only reading, which is the
# narrower of the two, and no vector contains non-ASCII whitespace.
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"


def normalize_line_endings(text: str) -> str:
    """Step 3, first half: CRLF and lone CR become LF."""
    # AMBIGUITY: the prose says "normalize line endings to LF" and does not
    # enumerate them. CRLF -> LF is beyond dispute; a lone CR is a line ending
    # in the classic-Mac sense and is normalized here. Vector
    # `line-endings-lone-cr` pins that reading on purpose so that a fork which
    # disagrees fails loudly instead of diverging quietly.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_string(text: str) -> str:
    """Step 3: normalize line endings, strip the outer edges, reflow nothing."""
    return normalize_line_endings(text).strip(ASCII_WHITESPACE)


def collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace to a single space (step 2 only)."""
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


def normalize_fingerprints(items: list) -> list:
    """Step 2: lowercase, strip, collapse runs, drop empties, dedup, sort."""
    normalized = []
    for item in items:
        if not isinstance(item, str):
            # Non-strings are schema violations, not canonicalization
            # questions. Pass them through untouched so a hash mismatch is not
            # mistaken for a normalization bug.
            normalized.append(item)
            continue
        # AMBIGUITY: str.lower() vs str.casefold() differ on e.g. "ß" and "İ".
        # lower() is the narrower reading of "lowercase every item"; no vector
        # contains a character where the two disagree.
        text = collapse_whitespace(
            normalize_line_endings(item).lower().strip(ASCII_WHITESPACE)
        )
        if text:
            normalized.append(text)
    # AMBIGUITY: "sort" is taken as an ordinary code-point sort. Locale or
    # Unicode-collation sorting would reorder non-ASCII fingerprints; the
    # non-ASCII vector keeps its fingerprints in an order where both readings
    # agree.
    return sorted(set(normalized))


def normalize_value(value, *, in_fingerprints: bool = False):
    """Recursively normalize one payload node."""
    if isinstance(value, dict):
        # Keys are schema-fixed identifiers and are never normalized: the
        # schema's additionalProperties:false means an odd key is rejected
        # rather than cleaned up.
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
    # null / bool / numbers pass through: `range.min: null` is a real value and
    # a numeric bound is a schema problem, not a hashing problem.
    return value


def canonical_payload(entry: dict) -> dict:
    """Steps 1-3: the normalized {"rule": ..., "scope": ...} payload."""
    missing = [key for key in PAYLOAD_KEYS if key not in entry]
    if missing:
        raise ValueError(f"entry is missing required payload keys: {missing}")
    # Absent optional keys stay absent. Nothing is defaulted in: an entry with
    # no `avoidance` must not hash like an entry with `avoidance: ""`.
    return {key: normalize_value(entry[key]) for key in PAYLOAD_KEYS}


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
