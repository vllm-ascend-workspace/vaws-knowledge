#!/usr/bin/env python3
"""``content_hash`` canonicalization, exactly as specified in docs/federation.md.

The revision identity of an entry is a hash over what the entry *claims*
(``scope`` + ``rule``), not over how it has been processed. Re-verifying,
re-reviewing or re-exporting an entry must not change its hash, otherwise
every federated sync becomes a revision storm.

The steps, restated from the contract so that a deviation is visible in review:

1. Take only ``scope`` and ``rule``. Nothing else.
2. ``rule.fingerprints``: lowercase, strip, collapse internal whitespace runs
   to one space, drop duplicates and empties, sort.
3. Every other string: strip leading/trailing whitespace, normalize line
   endings to LF. No other reflow.
4. Serialize ``{"rule": ..., "scope": ...}`` as JSON with ``sort_keys=True``,
   ``ensure_ascii=False`` and separators ``(",", ":")``.
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
_WS_RUN = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    """Step 3: strip ends, normalize line endings to LF, nothing else."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalize_fingerprints(items: Any) -> Any:
    """Step 2. Non-list values are passed through for the validator to reject."""
    if not isinstance(items, list):
        return items
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            # Leave the type error to schema validation; do not guess.
            return [i for i in items]
        norm = _WS_RUN.sub(" ", item.strip()).lower()
        if norm:
            seen.add(norm)
    return sorted(seen)


def _normalize_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_tree(v) for v in value]
    if isinstance(value, str):
        return _normalize_text(value)
    # bool / int / float / None are serialized as-is. Numbers should not
    # appear (versions are strings) and the validator says so explicitly.
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
    rule = _normalize_tree(entry["rule"])
    if isinstance(rule, dict) and "fingerprints" in rule:
        # Step 2 applies to the original items; re-normalizing the stripped
        # strings is idempotent so using the already-normalized list is fine.
        rule["fingerprints"] = _normalize_fingerprints(rule["fingerprints"])
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
