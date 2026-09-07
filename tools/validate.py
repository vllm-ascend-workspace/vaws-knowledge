#!/usr/bin/env python3
"""Validate knowledge corpus documents.

Two layers of checks:

1. JSON Schema (``schemas/knowledge-v2.schema.json``) via ``jsonschema``.
   Messages are rewritten where the raw schema error would not tell a
   contributor what to change (``oneOf`` on a scope dimension, undeclared
   fields, well-known patterns).
2. Checks the schema cannot express well:

   - ``layer`` must agree with the corpus directory holding the file
     (``corpus/verified/`` may only hold ``layer: verified``, and vice versa).
   - a ``layer: verified`` document must not carry ``status: unverified``
     entries.
   - ``uuid`` uniqueness across every file in the run.
   - ``content_hash`` equals the recomputation from ``tools/canonical.py``.
   - ``verification.verified_by`` must not be only the submitter (for
     ``verified`` / ``stale`` entries) and must never contain a bot identity.
   - ``verification.evidence[].ref`` must look like a reference, not prose.
   - version fields must be strings. YAML turns ``min: 2.5`` into a float; the
     message says so explicitly instead of a bare type error.
   - ``lifecycle.first_seen`` must not be after ``lifecycle.updated_at``.
   - ``supersedes`` / ``superseded_by`` / ``conflicts[].with`` must not point at
     the entry itself.

Exit codes: 0 clean, 1 problems found, 2 usage / dependency / unreadable input.

Usage::

    python3 tools/validate.py corpus/ examples/
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import canonical  # noqa: E402
from tools._common import (  # noqa: E402
    EXIT_FINDINGS,
    EXIT_OK,
    SCHEMA_PATH,
    ToolError,
    iter_corpus_files,
    json_pointer,
    load_document,
    load_schema,
    relpath,
    require_jsonschema,
    run_cli,
)

SCOPE_DIMENSIONS = (
    "soc", "cann", "driver", "python_abi", "torch", "torch_npu",
    "vllm", "vllm_ascend", "model", "topology", "execution_mode", "component",
)

CONCRETE_ENV_FIELDS = (
    "soc", "cann", "driver", "python_abi", "torch", "torch_npu",
    "vllm", "vllm_ascend", "model", "topology", "execution_mode",
)

#: Handles that are review automation, never a confirming human.
BOT_IDENTITY = re.compile(
    r"(?i)(?:\[bot\]$|(?:^|[^a-z])bot(?:$|[^a-z])|github-actions|dependabot|renovate|copilot|"
    r"^vaws-?(?:bot|ci|review)|^ci$)"
)

_URL = re.compile(r"^https?://\S+$")
_COMMIT_REF = re.compile(r"^(?:[\w.-]+/[\w.-]+@)?[0-9a-f]{7,64}$")
_ISSUE_REF = re.compile(r"^(?:[\w.-]+/[\w.-]+)?#\d+$")

_PATTERN_HINTS = {
    "uuid": "a lowercase RFC 4122 version-4 uuid, e.g. 1416a279-1215-4adf-a978-82b40a3be0bc",
    "content_hash": "sha256:<64 lowercase hex chars>; regenerate with tools/canonical.py",
    "slug": "lowercase letters, digits, '.', '_' and '-', starting with a letter or digit",
    "kind": "lowercase letters, digits and '-', starting with a letter or digit",
    "redaction_profile": "r<N>, e.g. r1 (the ruleset version printed by tools/redact.py --profile)",
    "updated_at": "an ISO date, YYYY-MM-DD, quoted so YAML keeps it a string",
    "first_seen": "an ISO date, YYYY-MM-DD, quoted so YAML keeps it a string",
    "submitted_at": "an ISO date, YYYY-MM-DD, quoted so YAML keeps it a string",
    "last_verified_at": "an ISO date, YYYY-MM-DD, quoted so YAML keeps it a string",
    "recorded_at": "an ISO date, YYYY-MM-DD, quoted so YAML keeps it a string",
}


@dataclass
class Problem:
    file: str
    path: str
    message: str

    def render(self) -> str:
        return f"{self.file}: {self.path}: {self.message}"


def _entry_label(entry: Any, index: int) -> str:
    if isinstance(entry, Mapping) and isinstance(entry.get("uuid"), str):
        return f"entries[{index}] (uuid {entry['uuid']})"
    return f"entries[{index}]"


# --------------------------------------------------------------------------- #
# Pre-pass: numeric version fields
# --------------------------------------------------------------------------- #

def _yaml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def find_numeric_version_fields(doc: Any) -> list[tuple[tuple[Any, ...], Any]]:
    """Return (path, value) for every version-ish field YAML parsed as a number.

    Covered: ``scope.<dim>.range.min|max``, ``scope.<dim>.values[]``, every
    field of ``verification.verified_against`` and ``rule.fingerprints[]``.
    """
    hits: list[tuple[tuple[Any, ...], Any]] = []
    if not isinstance(doc, Mapping) or not isinstance(doc.get("entries"), list):
        return hits

    def is_number(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for i, entry in enumerate(doc["entries"]):
        if not isinstance(entry, Mapping):
            continue
        scope = entry.get("scope")
        if isinstance(scope, Mapping):
            for dim, constraint in scope.items():
                if not isinstance(constraint, Mapping):
                    continue
                rng = constraint.get("range")
                if isinstance(rng, Mapping):
                    for bound in ("min", "max"):
                        if is_number(rng.get(bound)):
                            hits.append((("entries", i, "scope", dim, "range", bound), rng[bound]))
                values = constraint.get("values")
                if isinstance(values, list):
                    for j, v in enumerate(values):
                        if is_number(v):
                            hits.append((("entries", i, "scope", dim, "values", j), v))
        verification = entry.get("verification")
        if isinstance(verification, Mapping):
            against = verification.get("verified_against")
            if isinstance(against, Mapping):
                for k, v in against.items():
                    if is_number(v):
                        hits.append((("entries", i, "verification", "verified_against", k), v))
        rule = entry.get("rule")
        if isinstance(rule, Mapping) and isinstance(rule.get("fingerprints"), list):
            for j, v in enumerate(rule["fingerprints"]):
                if is_number(v):
                    hits.append((("entries", i, "rule", "fingerprints", j), v))
    return hits


# --------------------------------------------------------------------------- #
# Schema layer
# --------------------------------------------------------------------------- #

_validator_cache: dict[str, Any] = {}


def get_validator(schema_path: Path | None = None):
    jsonschema = require_jsonschema()
    key = str(schema_path or SCHEMA_PATH)
    if key not in _validator_cache:
        schema = load_schema(schema_path)
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        _validator_cache[key] = cls(schema)
    return _validator_cache[key]


def _describe_constraint_error(instance: Any) -> str:
    options = (
        "a scope dimension must be exactly one of: {any: true, basis: <why this fact is "
        "independent of the dimension, >= 12 chars>} | {values: [<exact strings>]} | "
        "{range: {min: <string|null>, max: <string|null>}}"
    )
    if not isinstance(instance, Mapping):
        return f"got {type(instance).__name__}; {options}"
    keys = set(instance)
    if "any" in keys:
        if instance.get("any") is not True:
            return "'any' must be the literal true; " + options
        if "basis" not in keys:
            return (
                "'any: true' requires a 'basis' explaining why the fact is independent of "
                "this dimension (an unexamined independence claim is what makes two forks' "
                "knowledge contradict)"
            )
        basis = instance.get("basis")
        if not isinstance(basis, str) or len(basis) < 12:
            return "'basis' must be a string of at least 12 characters stating a reviewable claim"
        extra = keys - {"any", "basis"}
        if extra:
            return f"'any' cannot be combined with {sorted(extra)}; " + options
    if "values" in keys:
        vals = instance.get("values")
        if not isinstance(vals, list) or not vals:
            return "'values' must be a non-empty list of strings"
        bad = [v for v in vals if not isinstance(v, str) or not v]
        if bad:
            return f"'values' items must be non-empty strings (offending: {bad!r})"
        extra = keys - {"values"}
        if extra:
            return f"'values' cannot be combined with {sorted(extra)}; " + options
    if "range" in keys:
        rng = instance.get("range")
        if not isinstance(rng, Mapping):
            return "'range' must be a mapping with 'min' and 'max'"
        missing = [b for b in ("min", "max") if b not in rng]
        if missing:
            return f"'range' is missing {missing}; use null for an unbounded side"
        extra = set(rng) - {"min", "max"}
        if extra:
            return f"'range' has undeclared field(s) {sorted(extra)}"
        for b in ("min", "max"):
            v = rng[b]
            if v is not None and not isinstance(v, str):
                return f"'range.{b}' must be a string or null, got {type(v).__name__}"
        extra = keys - {"range"}
        if extra:
            return f"'range' cannot be combined with {sorted(extra)}; " + options
    if not keys & {"any", "values", "range"}:
        return "dimension is declared but bounds nothing; " + options
    return options


def _format_schema_error(err: Any) -> str:
    path = list(err.absolute_path)
    last = path[-1] if path else None
    validator = err.validator

    if validator == "additionalProperties":
        extra = sorted(
            k for k in (err.instance or {}) if k not in (err.schema.get("properties") or {})
        ) if isinstance(err.instance, Mapping) else []
        return (
            f"undeclared field(s) {extra}: the schema is the egress whitelist, so an "
            "undeclared field cannot be published — remove it or propose a schema change"
        )
    if validator == "required":
        m = re.match(r"'(.+)' is a required property", err.message)
        name = m.group(1) if m else err.message
        # Distinguish the conditional requirements from plain ones.
        if name == "verification":
            return "missing 'verification': status 'verified' and 'stale' both assert the claim was established, so they need evidence, verified_by, verified_against and last_verified_at"
        if name == "resolved_by":
            return "missing 'lifecycle.resolved_by': a resolved entry must point at the pull_request / commit / release that fixed it"
        if name in SCOPE_DIMENSIONS and last == "scope":
            return (
                f"missing scope dimension '{name}': all twelve dimensions are required; "
                "bound it (values / range) or declare {any: true, basis: ...}"
            )
        return f"missing required field '{name}'"
    if validator == "oneOf" and len(path) >= 2 and path[-2] == "scope":
        return _describe_constraint_error(err.instance)
    if validator == "oneOf":
        return f"value matches none of the allowed shapes: {err.message}"
    if validator == "minItems":
        if last == "evidence":
            return "'verification.evidence' must have at least one reference for a verified/stale entry — prose is not evidence; submit as 'unverified' if the run is not referenceable"
        if last == "verified_by":
            return "'verification.verified_by' must name at least one confirming handle for a verified/stale entry"
        return err.message
    if validator == "pattern":
        hint = _PATTERN_HINTS.get(str(last))
        return f"{err.message}" + (f" — expected {hint}" if hint else "")
    if validator == "enum":
        if last == "status" and set(err.validator_value or ()) == {"verified", "stale", "resolved"}:
            return f"{err.message}: confidence 'high' is only allowed with status verified / stale / resolved"
        return err.message
    if validator == "const" and last == "schema_version":
        return "schema_version must be the integer 2"
    if validator == "type":
        return err.message
    if validator == "minLength":
        return f"{err.message} (empty or too short)"
    return err.message


def schema_problems(doc: Any, file: str, skip_paths: Iterable[tuple[Any, ...]] = (), schema_path: Path | None = None) -> list[Problem]:
    validator = get_validator(schema_path)
    skip = {tuple(p) for p in skip_paths}
    problems: list[Problem] = []
    errors = sorted(validator.iter_errors(doc), key=lambda e: (list(map(str, e.absolute_path)), e.validator))
    for err in errors:
        path = tuple(err.absolute_path)
        if path in skip:
            continue
        problems.append(Problem(file, json_pointer(path), _format_schema_error(err)))
    return problems


def _entry_validator(schema_path: Path | None = None):
    """Validator for one entry, still resolving $defs from the real schema."""
    jsonschema = require_jsonschema()
    key = "entry:" + str(schema_path or SCHEMA_PATH)
    if key not in _validator_cache:
        schema = load_schema(schema_path)
        cls = jsonschema.validators.validator_for(schema)
        entry_schema = {
            "$schema": schema.get("$schema"),
            "$defs": schema.get("$defs", {}),
            "$ref": "#/$defs/entry",
        }
        _validator_cache[key] = cls(entry_schema)
    return _validator_cache[key]


def _numeric_skips(numeric: list[tuple[tuple[Any, ...], Any]]) -> list[tuple[Any, ...]]:
    skip: list[tuple[Any, ...]] = []
    for path, _ in numeric:
        skip.append(path)
        if len(path) >= 4 and path[2] == "scope":
            skip.append(path[:4])
    return skip


def _entry_schema_problems(
    entry: Any,
    file: str,
    index: int,
    skip_paths: Iterable[tuple[Any, ...]] = (),
    schema_path: Path | None = None,
) -> list[Problem]:
    if not isinstance(entry, Mapping):
        return [Problem(file, f"entries[{index}]", f"entry must be a mapping, got {type(entry).__name__}")]
    skip = {tuple(p) for p in skip_paths}
    problems: list[Problem] = []
    validator = _entry_validator(schema_path)
    errors = sorted(
        validator.iter_errors(entry),
        key=lambda e: (list(map(str, e.absolute_path)), e.validator),
    )
    for err in errors:
        rel = tuple(err.absolute_path)
        path = ("entries", index, *rel)
        if path in skip or rel in skip:
            continue
        problems.append(Problem(file, json_pointer(path), _format_schema_error(err)))
    return problems


def structural_type_problems(doc: Any, file: str, schema_path: Path | None = None) -> list[Problem]:
    """Schema structural/type problems, including YAML numbers parsed as floats.

    This is step 0 for tools/canonical.py: reject invalid types before a hash
    is printed. It does **not** compare declared ``content_hash`` to a
    recomputation, so a stale derived hash on an otherwise schema-valid entry
    can still be regenerated.
    """
    problems: list[Problem] = []
    if isinstance(doc, Mapping) and isinstance(doc.get("entries"), list):
        numeric = find_numeric_version_fields(doc)
        for path, value in numeric:
            problems.append(Problem(
                file, json_pointer(path),
                f"YAML parsed this value as a number ({_yaml_literal(value)}); version and "
                f"environment fields must be strings. Quote it, e.g. {path[-1] if isinstance(path[-1], str) else 'value'}: '{value}'",
            ))
        problems.extend(
            schema_problems(doc, file, skip_paths=_numeric_skips(numeric), schema_path=schema_path)
        )
        return problems

    if isinstance(doc, list):
        entries = doc
        wrapped = {"entries": entries}
        index_base = None
    elif isinstance(doc, Mapping):
        entries = [doc]
        wrapped = {"entries": entries}
        index_base = 0
    else:
        return [Problem(
            file, "<document>",
            f"document must be a mapping or a list of entries, got {type(doc).__name__}",
        )]

    numeric = find_numeric_version_fields(wrapped)
    for path, value in numeric:
        problems.append(Problem(
            file, json_pointer(path),
            f"YAML parsed this value as a number ({_yaml_literal(value)}); version and "
            f"environment fields must be strings. Quote it, e.g. {path[-1] if isinstance(path[-1], str) else 'value'}: '{value}'",
        ))
    skip = _numeric_skips(numeric)
    for i, entry in enumerate(entries):
        problems.extend(
            _entry_schema_problems(entry, file, i if index_base is None else index_base, skip, schema_path)
        )
    return problems


# --------------------------------------------------------------------------- #
# Cross checks
# --------------------------------------------------------------------------- #

def expected_layer_for(path: Path) -> str | None:
    """``corpus/verified/...`` -> 'verified'; ``corpus/unverified/...`` -> 'unverified'."""
    parts = path.resolve().parts
    for i in range(len(parts) - 1):
        if parts[i] == "corpus" and parts[i + 1] in ("verified", "unverified"):
            return parts[i + 1]
    return None


def _looks_like_prose(ref: str) -> bool:
    return bool(re.search(r"\s", ref.strip())) or ref.strip() != ref


def evidence_problems(entry: Mapping[str, Any], file: str, base: str) -> list[Problem]:
    out: list[Problem] = []
    verification = entry.get("verification")
    if not isinstance(verification, Mapping):
        return out
    evidence = verification.get("evidence")
    if not isinstance(evidence, list):
        return out
    for j, ev in enumerate(evidence):
        if not isinstance(ev, Mapping):
            continue  # schema reports the type error
        ref = ev.get("ref")
        etype = ev.get("type")
        where = f"{base}.verification.evidence[{j}].ref"
        if not isinstance(ref, str):
            continue
        if _looks_like_prose(ref):
            out.append(Problem(file, where, "evidence ref must be a followable reference (manifest id, owner/repo#N, commit sha, URL), not prose — move the narrative to 'note' or drop it"))
            continue
        if etype == "commit" and not (_COMMIT_REF.match(ref) or _URL.match(ref)):
            out.append(Problem(file, where, "commit evidence must be a hex sha (7-64 chars), optionally 'owner/repo@sha', or a commit URL"))
        elif etype in ("pull_request", "issue") and not (_ISSUE_REF.match(ref) or _URL.match(ref)):
            out.append(Problem(file, where, f"{etype} evidence must be 'owner/repo#N', '#N' or a URL"))
    return out


def verified_by_problems(entry: Mapping[str, Any], file: str, base: str, layer: Any = None) -> list[Problem]:
    """Bot identities are never valid. The submitter alone is not enough once
    the entry claims to have been established (status verified / stale) or
    sits in the shared layer (layer verified)."""
    out: list[Problem] = []
    verification = entry.get("verification")
    if not isinstance(verification, Mapping):
        return out
    verified_by = verification.get("verified_by")
    if not isinstance(verified_by, list) or not all(isinstance(v, str) for v in verified_by):
        return out
    where = f"{base}.verification.verified_by"
    bots = [v for v in verified_by if BOT_IDENTITY.search(v)]
    if bots:
        out.append(Problem(file, where, f"contains bot identity {bots}: a review bot cannot confirm a technical claim; only human handles are valid here"))
    status = entry.get("status")
    if status in ("verified", "stale") or layer == "verified":
        provenance = entry.get("provenance")
        contributor = provenance.get("contributor") if isinstance(provenance, Mapping) else None
        humans = [v for v in verified_by if v not in bots]
        others = [v for v in humans if v != contributor]
        if humans and not others:
            why = f"status '{status}'" if status in ("verified", "stale") else "layer 'verified'"
            out.append(Problem(file, where, f"only the submitter ({contributor!r}) confirmed this entry; {why} requires confirmation from someone other than the submitter"))
    return out


def lifecycle_problems(entry: Mapping[str, Any], file: str, base: str) -> list[Problem]:
    out: list[Problem] = []
    lifecycle = entry.get("lifecycle")
    uuid = entry.get("uuid")
    if isinstance(lifecycle, Mapping):
        first_seen = lifecycle.get("first_seen")
        updated_at = lifecycle.get("updated_at")
        if isinstance(first_seen, str) and isinstance(updated_at, str) and first_seen > updated_at:
            out.append(Problem(file, f"{base}.lifecycle", f"first_seen {first_seen} is after updated_at {updated_at}"))
        if isinstance(uuid, str):
            if lifecycle.get("superseded_by") == uuid:
                out.append(Problem(file, f"{base}.lifecycle.superseded_by", "an entry cannot supersede itself"))
            supersedes = lifecycle.get("supersedes")
            if isinstance(supersedes, list) and uuid in supersedes:
                out.append(Problem(file, f"{base}.lifecycle.supersedes", "an entry cannot supersede itself"))
    conflicts = entry.get("conflicts")
    if isinstance(conflicts, list) and isinstance(uuid, str):
        for j, c in enumerate(conflicts):
            if isinstance(c, Mapping) and c.get("with") == uuid:
                out.append(Problem(file, f"{base}.conflicts[{j}].with", "an entry cannot conflict with itself"))
    return out


def hash_problems(entry: Mapping[str, Any], file: str, base: str) -> list[Problem]:
    declared = entry.get("content_hash")
    if not isinstance(declared, str) or "scope" not in entry or "rule" not in entry:
        return []
    try:
        actual = canonical.content_hash(entry)
    except ToolError:
        return []
    if declared != actual:
        return [Problem(file, f"{base}.content_hash", f"declared {declared} but the canonical scope+rule payload hashes to {actual}; regenerate with tools/canonical.py (or tools/export.py), do not edit by hand")]
    return []


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

@dataclass
class ValidationResult:
    problems: list[Problem]
    files: int
    entries: int

    @property
    def ok(self) -> bool:
        return not self.problems


def validate_document(doc: Any, file: str, expected_layer: str | None = None, schema_path: Path | None = None) -> tuple[list[Problem], int]:
    """Validate one parsed document. Returns (problems, entry_count)."""
    problems: list[Problem] = []
    if not isinstance(doc, Mapping):
        problems.append(Problem(file, "<document>", f"document must be a mapping with schema_version/kind/layer/updated_at/entries, got {type(doc).__name__}"))
        return problems, 0

    numeric = find_numeric_version_fields(doc)
    for path, value in numeric:
        problems.append(Problem(
            file, json_pointer(path),
            f"YAML parsed this value as a number ({_yaml_literal(value)}); version and "
            f"environment fields must be strings. Quote it, e.g. {path[-1] if isinstance(path[-1], str) else 'value'}: '{value}'",
        ))
    # A numeric bound also makes the enclosing scope dimension fail its oneOf;
    # that second message would only restate the first, so skip it too.
    skip: list[tuple[Any, ...]] = []
    for path, _ in numeric:
        skip.append(path)
        if len(path) >= 4 and path[2] == "scope":
            skip.append(path[:4])
    problems.extend(schema_problems(doc, file, skip_paths=skip, schema_path=schema_path))

    layer = doc.get("layer")
    if expected_layer and layer in ("verified", "unverified") and layer != expected_layer:
        problems.append(Problem(file, "layer", f"file lives under corpus/{expected_layer}/ but declares layer: {layer}; corpus/{expected_layer}/ may only hold layer: {expected_layer}"))

    entries = doc.get("entries")
    if not isinstance(entries, list):
        return problems, 0
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        base = _entry_label(entry, i)
        if layer == "verified" and entry.get("status") == "unverified":
            problems.append(Problem(file, f"{base}.status", "a layer: verified document cannot hold status: unverified entries; they belong in corpus/unverified/"))
        problems.extend(hash_problems(entry, file, base))
        problems.extend(evidence_problems(entry, file, base))
        problems.extend(verified_by_problems(entry, file, base, layer))
        problems.extend(lifecycle_problems(entry, file, base))
    return problems, len(entries)


def validate_paths(paths: Sequence[str], schema_path: Path | None = None) -> ValidationResult:
    problems: list[Problem] = []
    files = 0
    entries = 0
    seen_uuid: dict[str, str] = {}
    for path in iter_corpus_files(paths):
        files += 1
        label = relpath(path)
        try:
            doc = load_document(path)
        except ToolError as exc:
            problems.append(Problem(label, "<document>", str(exc).split(": ", 1)[-1]))
            continue
        doc_problems, count = validate_document(doc, label, expected_layer_for(path), schema_path)
        problems.extend(doc_problems)
        entries += count
        if isinstance(doc, Mapping) and isinstance(doc.get("entries"), list):
            for i, entry in enumerate(doc["entries"]):
                if not isinstance(entry, Mapping):
                    continue
                uuid = entry.get("uuid")
                if not isinstance(uuid, str):
                    continue
                where = f"{label}: {_entry_label(entry, i)}"
                if uuid in seen_uuid:
                    problems.append(Problem(label, f"{_entry_label(entry, i)}.uuid", f"duplicate uuid; first declared at {seen_uuid[uuid]}. uuid is identity across the whole corpus — a revision of an existing entry keeps its uuid and changes content_hash, a different claim needs a new uuid4"))
                else:
                    seen_uuid[uuid] = where
    return ValidationResult(problems, files, entries)


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate knowledge documents against schemas/knowledge-v2.schema.json and the cross-entry rules.",
    )
    parser.add_argument("paths", nargs="+", help="YAML/JSON files or directories")
    parser.add_argument("--schema", type=Path, default=None, help=f"schema file (default: {SCHEMA_PATH.relative_to(SCHEMA_PATH.parent.parent)})")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress the summary line")
    args = parser.parse_args(argv)

    result = validate_paths(args.paths, args.schema)
    for p in result.problems:
        print(p.render())
    if not args.quiet:
        status = "OK" if result.ok else "FAILED"
        print(f"validate: {result.files} file(s), {result.entries} entr{'y' if result.entries == 1 else 'ies'}, {len(result.problems)} problem(s) — {status}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_FINDINGS


if __name__ == "__main__":
    run_cli(main)
