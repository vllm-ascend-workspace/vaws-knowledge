#!/usr/bin/env python3
"""``content_hash`` canonicalization, exactly as specified in docs/federation.md.

The revision identity of an entry is a hash over what the entry *claims*
(``scope`` + ``rule``), not over how it has been processed. Re-verifying,
re-reviewing or re-exporting an entry must not change its hash, otherwise
every federated sync becomes a revision storm.

The steps, restated from the contract so that a deviation is visible in review:

0. Validate first. Never coerce types: a YAML ``min: 2.5`` is a float, a
   non-string fingerprint item is not a signature, and a non-string mapping
   key is not a schema field. Reject those at this CLI and at any caller that
   would otherwise publish a hash.
1. Take only ``scope`` and ``rule``. Nothing else.
2. ``rule.fingerprints``: lowercase ASCII letters only, strip leading and
   trailing ASCII whitespace, collapse internal runs of ASCII whitespace to
   one ``U+0020``, drop duplicates and empties, sort byte-wise over UTF-8.
3. Every other string value, recursively at any depth: normalize line endings
   to LF, strip trailing ASCII whitespace from every line, then strip leading
   and trailing ASCII whitespace from the whole value. Mapping keys are not
   touched. Non-ASCII spaces (NBSP, U+3000) are content.
4. Serialize ``{"rule": ..., "scope": ...}`` as JSON with ``sort_keys=True``,
   ``ensure_ascii=False`` and separators ``(",", ":")``. No Unicode
   normalization form is applied.
5. ``content_hash = "sha256:" + sha256(utf8(json)).hexdigest()``.

Cross-implementation anchor: the entry in ``examples/valid-entry.yaml`` must
hash to ``sha256:32d1e6611f47083c885205b4f4ef398ea238c0e7eaa60a3e3d961c49ce5b166a``.
``tests/test_tools_canonical.py`` asserts this.

CLI: ``python3 tools/canonical.py FILE...`` prints ``uuid  content_hash`` per
entry, or the payload with ``--payload``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._common import (  # noqa: E402
    EXIT_OK,
    ToolError,
    iter_corpus_files,
    load_document,
    relpath,
    run_cli,
)

HASH_PREFIX = "sha256:"
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ASCII_WS_RUN = re.compile(r"[ \t\n\r\x0b\x0c]+")


def _ascii_lower(value: str) -> str:
    """Step 2: A–Z → a–z only. Non-ASCII letters are left unchanged."""
    return value.translate(_ASCII_LOWER)


def _normalize_text(value: str) -> str:
    """Step 3: LF endings, per-line trailing ASCII whitespace, then outer ASCII strip."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip(ASCII_WHITESPACE) for line in text.split("\n"))
    return text.strip(ASCII_WHITESPACE)


def _normalize_fingerprints(items: Any) -> Any:
    """Step 2. Non-list values are passed through for the validator to reject."""
    if not isinstance(items, list):
        return items
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            # Leave the type error to schema validation; do not guess.
            return [i for i in items]
        norm = _ASCII_WS_RUN.sub(" ", _ascii_lower(item).strip(ASCII_WHITESPACE))
        if norm:
            seen.add(norm)
    return sorted(seen, key=lambda s: s.encode("utf-8"))


def _payload_type_error(value: Any, path: str, *, in_fingerprints: bool = False) -> str | None:
    """Step 0: name a type that must not be coerced into a published hash."""
    if isinstance(value, Mapping):
        for key, sub in value.items():
            if not isinstance(key, str):
                return (
                    f"{path}: mapping keys must be strings, got {type(key).__name__} "
                    f"{key!r}; canonicalization does not stringify keys"
                )
            child = f"{path}.{key}"
            if key == "fingerprints" and not isinstance(sub, list):
                return (
                    f"{child} must be a list of strings, got {type(sub).__name__}; "
                    "canonicalization does not stringify fingerprint items"
                )
            err = _payload_type_error(sub, child, in_fingerprints=(key == "fingerprints"))
            if err:
                return err
        return None
    if isinstance(value, list):
        if in_fingerprints:
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    return (
                        f"{path}[{index}]: fingerprint items must be strings, got "
                        f"{type(item).__name__} {item!r}; canonicalization does not "
                        "stringify them"
                    )
            return None
        for index, item in enumerate(value):
            err = _payload_type_error(item, f"{path}[{index}]")
            if err:
                return err
        return None
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    if isinstance(value, (int, float)):
        return (
            f"{path}: numeric value {value!r} is a {type(value).__name__} and is not "
            "canonicalized; quote it as a string (canonicalization does not stringify types)"
        )
    return (
        f"{path}: unsupported type {type(value).__name__}; "
        "canonicalization does not coerce it"
    )


def _normalize_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if not isinstance(key, str):
                raise ToolError(
                    "canonical_payload: mapping keys must be strings, got "
                    f"{type(key).__name__} {key!r}; canonicalization does not stringify keys"
                )
            out[key] = _normalize_tree(sub)
        return out
    if isinstance(value, list):
        return [_normalize_tree(v) for v in value]
    if isinstance(value, str):
        return _normalize_text(value)
    # bool / None pass through. Numbers are rejected in canonical_payload
    # before this walk so they cannot be stringified into a published hash.
    return value


def canonical_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical ``{"rule": ..., "scope": ...}`` payload for ``entry``.

    ``entry`` is a single entry mapping (one element of a document's
    ``entries``). Missing ``scope`` or ``rule`` raise ``ToolError`` rather than
    hashing a partial payload, because a hash over half an entry would collide
    with nothing and silently look like a legitimate revision.
    """
    if not isinstance(entry, Mapping):
        raise ToolError("canonical_payload: entry must be a mapping")
    missing = [k for k in ("scope", "rule") if k not in entry]
    if missing:
        raise ToolError(
            "canonical_payload: entry is missing "
            + ", ".join(f"'{m}'" for m in missing)
            + "; content_hash is defined over scope + rule only and cannot be "
            "computed without both"
        )
    for key in ("scope", "rule"):
        if not isinstance(entry[key], Mapping):
            raise ToolError(
                f"canonical_payload: {key} must be a mapping, got "
                f"{type(entry[key]).__name__}"
            )
        err = _payload_type_error(entry[key], key)
        if err:
            raise ToolError("canonical_payload: " + err)
    rule = _normalize_tree(entry["rule"])
    if isinstance(rule, dict) and "fingerprints" in rule:
        # Step 2 applies to the original fingerprint strings, not to the
        # already step-3-normalized copies in the walked tree.
        rule["fingerprints"] = _normalize_fingerprints(entry["rule"].get("fingerprints"))
    scope = _normalize_tree(entry["scope"])
    return {"rule": rule, "scope": scope}


def canonical_json(entry: Mapping[str, Any]) -> str:
    """Step 4: the exact string that is hashed."""
    return json.dumps(
        canonical_payload(entry),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def content_hash(entry: Mapping[str, Any]) -> str:
    """Step 5: ``sha256:<hex>`` over the UTF-8 canonical JSON."""
    digest = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
    return HASH_PREFIX + digest


def _entries_of(doc: Any) -> list[Any]:
    if isinstance(doc, Mapping) and isinstance(doc.get("entries"), list):
        return doc["entries"]
    if isinstance(doc, list):
        return doc
    if isinstance(doc, Mapping) and "uuid" in doc:
        return [doc]
    return []


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="canonical.py",
        description="Compute content_hash for entries in knowledge documents.",
    )
    parser.add_argument("paths", nargs="+", help="YAML/JSON files or directories")
    parser.add_argument(
        "--payload",
        action="store_true",
        help="print the canonical JSON payload instead of the hash",
    )
    args = parser.parse_args(argv)

    for path in iter_corpus_files(args.paths):
        doc = load_document(path)
        for index, entry in enumerate(_entries_of(doc)):
            label = entry.get("uuid", f"entries[{index}]") if isinstance(entry, Mapping) else f"entries[{index}]"
            if args.payload:
                print(f"# {relpath(path)} {label}")
                print(canonical_json(entry))
            else:
                print(f"{relpath(path)}\t{label}\t{content_hash(entry)}")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
