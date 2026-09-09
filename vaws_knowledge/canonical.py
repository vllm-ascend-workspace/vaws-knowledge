#!/usr/bin/env python3
"""``content_hash`` canonicalization, exactly as specified in docs/federation.md.

The revision identity of an entry is a hash over what the entry *claims*
(``scope`` + ``rule``), not over how it has been processed. Re-verifying,
re-reviewing or re-exporting an entry must not change its hash, otherwise
every federated sync becomes a revision storm.

The steps, restated from the contract so that a deviation is visible in review:

0. Validate first. The CLI runs the existing schema structural/type checks
   (via ``vaws_knowledge.validate``) before printing a hash or payload. It does not
   require the stored ``content_hash`` to already match, so a stale derived
   hash can still be recomputed. Low-level helpers still refuse to stringify
   types; they are not a public validation shortcut.
1. Take the entry's single body, plus ``scope`` when the body is a runtime
   claim. The body is ``rule`` for a failure rule, ``measurement`` for a
   measured or vendor-declared quantity, or ``reference`` for sourced
   material. The payload key is the body's own name, so a rule entry hashes
   byte-for-byte as it always did. A sourced reference hashes ``{"reference":
   …}`` only: it must not invent the twelve runtime coordinates.
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

CLI: ``vaws-knowledge canonical FILE...`` prints ``uuid  content_hash`` per
entry, or the payload with ``--payload``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from vaws_knowledge._common import (  # noqa: E402
    EXIT_FINDINGS,
    EXIT_OK,
    ToolError,
    iter_corpus_files,
    load_document,
    relpath,
    run_cli,
)

HASH_PREFIX = "sha256:"

#: The two entry body variants, in the order a payload key is looked for.
#: docs/federation.md step 1 takes ``scope`` plus the body; the payload key is
#: the body's own name, which is what keeps every pre-existing rule hash
#: unchanged.
BODY_KEYS = ("rule", "measurement", "reference")
RUNTIME_BODY_KEYS = ("rule", "measurement")

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


def body_key(entry: Mapping[str, Any]) -> str | None:
    """Name of the entry's single body key, or ``None`` if it has not got one.

    Exactly one of ``rule`` / ``measurement`` / ``reference`` is expected.
    Two bodies is ambiguous rather than richer, so it is refused here as well
    as by the schema: hashing both under one revision would let a change to
    either look like a change to the entry as a whole.
    """
    if not isinstance(entry, Mapping):
        return None
    present = [key for key in BODY_KEYS if key in entry]
    return present[0] if len(present) == 1 else None


def canonical_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical ``{<body>: ..., "scope": ...}`` payload for ``entry``.

    ``entry`` is a single entry mapping (one element of a document's
    ``entries``). The body key is ``rule``, ``measurement`` or ``reference``.
    Runtime bodies still require ``scope``. A sourced reference must not
    carry ``scope``: hashing invented coordinates would look like a runtime
    claim. A missing body or two bodies raise ``ToolError`` rather than
    hashing a partial payload.
    """
    if not isinstance(entry, Mapping):
        raise ToolError("canonical_payload: entry must be a mapping")
    body = body_key(entry)
    if body is None:
        present = [key for key in BODY_KEYS if key in entry]
        if len(present) > 1:
            raise ToolError(
                "canonical_payload: entry declares both "
                + " and ".join(f"'{p}'" for p in present)
                + "; an entry has exactly one body and content_hash cannot cover two"
            )
        raise ToolError(
            "canonical_payload: entry has no body; content_hash is defined over "
            "scope + one of " + ", ".join(f"'{k}'" for k in RUNTIME_BODY_KEYS)
            + f", or over '{BODY_KEYS[-1]}' alone"
        )
    if body in RUNTIME_BODY_KEYS and "scope" not in entry:
        raise ToolError(
            "canonical_payload: entry is missing 'scope'; content_hash is defined "
            f"over scope + {body} and cannot be computed without both"
        )
    if body == "reference" and "scope" in entry:
        raise ToolError(
            "canonical_payload: a sourced reference must not declare 'scope'; "
            "the twelve runtime coordinates are for rules and measurements only"
        )
    keys = (body,) if body == "reference" else ("scope", body)
    for key in keys:
        if not isinstance(entry[key], Mapping):
            raise ToolError(
                f"canonical_payload: {key} must be a mapping, got "
                f"{type(entry[key]).__name__}"
            )
        err = _payload_type_error(entry[key], key)
        if err:
            raise ToolError("canonical_payload: " + err)
    body_tree = _normalize_tree(entry[body])
    if isinstance(body_tree, dict) and "fingerprints" in body_tree:
        # Step 2 applies to the original fingerprint strings, not to the
        # already step-3-normalized copies in the walked tree.
        body_tree["fingerprints"] = _normalize_fingerprints(entry[body].get("fingerprints"))
    if body == "reference":
        return {body: body_tree}
    scope = _normalize_tree(entry["scope"])
    return {body: body_tree, "scope": scope}


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


def _entry_from_stdin() -> Mapping[str, Any]:
    """One entry on stdin, for ``--hash-cmd "vaws-knowledge canonical"``."""
    text = sys.stdin.read()
    if not text.strip():
        raise ToolError("canonical: no entry on stdin")
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        data = json.loads(stripped)
    else:
        from vaws_knowledge._common import require_yaml  # noqa: PLC0415

        data = require_yaml().safe_load(text)
    if isinstance(data, Mapping) and "entries" in data:
        entries = _entries_of(data)
        if len(entries) != 1:
            raise ToolError("canonical: stdin document must contain exactly one entry")
        return entries[0]
    if isinstance(data, Mapping):
        return data
    raise ToolError("canonical: stdin is not an entry")


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="vaws-knowledge canonical",
        description="Compute content_hash for entries in knowledge documents.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="YAML/JSON files or directories; omit or pass - to read one entry from stdin",
    )
    parser.add_argument(
        "--payload",
        action="store_true",
        help="print the canonical JSON payload instead of the hash",
    )
    args = parser.parse_args(argv)

    if not args.paths or args.paths == ["-"]:
        entry = _entry_from_stdin()
        if args.payload:
            print(canonical_json(entry))
        else:
            print(content_hash(entry))
        return EXIT_OK

    # Import lazily: validate imports this module at load time.
    from vaws_knowledge import validate  # noqa: PLC0415

    loaded: list[tuple[Path, Any]] = []
    problems: list[Any] = []
    for path in iter_corpus_files(args.paths):
        doc = load_document(path)
        problems.extend(validate.structural_type_problems(doc, relpath(path)))
        loaded.append((path, doc))
    if problems:
        for problem in problems:
            print(problem.render(), file=sys.stderr)
        print(
            "canonical: refusing to publish a hash or payload for schema-invalid input",
            file=sys.stderr,
        )
        return EXIT_FINDINGS

    for path, doc in loaded:
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
