#!/usr/bin/env python3
"""Egress gate: turn a fork's local knowledge into a v2 document ready to propose.

Nothing leaves a contributing fork except through this tool, in this order:

1. **Whitelist.** Every entry is walked against the schema. A field the schema
   does not declare is refused (default) or dropped (``--drop-undeclared``);
   it is never passed through. ``additionalProperties: false`` in the schema
   is the privacy boundary and this step is how it is enforced *before*
   validation, so a leak in an undeclared field cannot even reach the
   redaction report.
2. **Stamp.** ``provenance`` is completed (``contributor``, ``origin_repo``,
   ``submitted_at``) and ``provenance.redaction_profile`` is set to the ruleset
   this run clears the entry under. ``content_hash`` is recomputed from the
   canonical ``scope`` + ``rule`` payload.
3. **Validate.** The assembled document must pass ``tools/validate.py``.
4. **Redact.** The assembled document is scanned with ``tools/redact.py`` in
   check mode. Any finding refuses the export.
5. **Serialize deterministically.** Keys in schema order, entries sorted by
   ``uuid``, fixed YAML style. Re-exporting unchanged input yields
   byte-identical output.

Idempotency and dates
---------------------
``provenance.submitted_at`` is the one field that would otherwise change on
every run. Precedence: a value already present on the input entry, then a
matching entry (same ``uuid`` **and** same ``content_hash``) in ``--previous``,
then ``--submitted-at``, then today's UTC date. The document-level
``updated_at`` is derived as the newest ``lifecycle.updated_at`` across the
entries, so it never depends on the clock.

Input shapes accepted: a v2 document (``entries: [...]``), a bare list of
entries, or a single entry mapping. Several files may be given; they must agree
on ``kind`` (or ``--kind`` must be passed).

What this tool does *not* do: migrate v1 (free-text ``applicable_versions``)
entries. Turning prose applicability into twelve reviewed scope dimensions is
authorship, not conversion.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import canonical, redact, validate  # noqa: E402
from tools._common import (  # noqa: E402
    EXIT_FINDINGS,
    EXIT_OK,
    ToolError,
    json_pointer,
    load_document,
    load_schema,
    require_yaml,
    run_cli,
)

DEFAULT_LAYER = "unverified"
DEFAULT_CONTRIBUTOR = "anonymous"

# Key order for serialization, per object. Anything not listed is appended in
# sorted order (cannot happen after whitelisting, but keeps the dump total).
_DOC_ORDER = ("schema_version", "kind", "layer", "updated_at", "entries")
_ENTRY_ORDER = (
    "uuid", "slug", "content_hash", "status", "confidence", "scope",
    "provenance", "verification", "lifecycle", "conflicts", "rule",
)
_ORDER_BY_KEY = {
    "scope": validate.SCOPE_DIMENSIONS,
    "provenance": ("contributor", "origin_repo", "submitted_at", "redaction_profile"),
    "verification": ("evidence", "verified_by", "verified_against", "last_verified_at"),
    "verified_against": validate.CONCRETE_ENV_FIELDS,
    "lifecycle": ("first_seen", "updated_at", "supersedes", "superseded_by", "resolved_by"),
    "rule": ("summary", "symptom", "root_cause", "resolution", "avoidance", "fingerprints"),
    "range": ("min", "max"),
}
_CONSTRAINT_ORDER = ("any", "basis", "values", "range")
_EVIDENCE_ORDER = ("type", "ref", "note")
_CONFLICT_ORDER = ("with", "undeclared_dimensions", "recorded_at", "note")
_RESOLVED_BY_ORDER = ("type", "ref")


# --------------------------------------------------------------------------- #
# Whitelist walk
# --------------------------------------------------------------------------- #

class _SchemaWalker:
    """Collect paths of fields the schema does not declare.

    ``oneOf`` / ``anyOf`` / ``allOf`` branches are unioned: a key declared by
    any branch is "declared". Whether the *combination* is valid is the
    validator's job afterwards; this walk only answers "may this key exist at
    all", which is the egress question.
    """

    def __init__(self, schema: Mapping[str, Any]):
        self.schema = schema
        self.defs = schema.get("$defs", {})

    def _resolve(self, node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref.startswith("#/$defs/"):
                target = self.defs.get(ref[len("#/$defs/"):])
                if isinstance(target, Mapping):
                    out.extend(self._resolve(target))
            elif ref == "#":
                out.extend(self._resolve(self.schema))
        own = {k: v for k, v in node.items() if k != "$ref"}
        if own:
            out.append(own)
        for comb in ("oneOf", "anyOf", "allOf"):
            for branch in node.get(comb, ()) or ():
                if isinstance(branch, Mapping):
                    out.extend(self._resolve(branch))
        for cond in ("then", "else"):
            branch = node.get(cond)
            if isinstance(branch, Mapping):
                out.extend(self._resolve(branch))
        return out

    def undeclared(self, instance: Any, node: Mapping[str, Any], path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        nodes = self._resolve(node)
        found: list[tuple[Any, ...]] = []
        if isinstance(instance, Mapping):
            declared: dict[str, list[Mapping[str, Any]]] = {}
            closed = False
            for n in nodes:
                props = n.get("properties")
                if isinstance(props, Mapping):
                    for k, sub in props.items():
                        declared.setdefault(k, []).append(sub)
                if n.get("additionalProperties") is False:
                    closed = True
            if not closed and not declared:
                return found  # free-form object: nothing to enforce here
            for key, value in instance.items():
                if key not in declared:
                    found.append(path + (key,))
                    continue
                # A key may be described by several partial schemas (the full
                # $ref plus an if/then refinement). They must be read as one
                # declaration, not checked one by one.
                subs = declared[key]
                merged = subs[0] if len(subs) == 1 else {"allOf": subs}
                found.extend(self.undeclared(value, merged, path + (key,)))
        elif isinstance(instance, list):
            for n in nodes:
                items = n.get("items")
                if isinstance(items, Mapping):
                    for i, v in enumerate(instance):
                        found.extend(self.undeclared(v, items, path + (i,)))
        # de-duplicate while keeping order
        seen: set[tuple[Any, ...]] = set()
        uniq = []
        for p in found:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq


def undeclared_fields(entry: Mapping[str, Any], schema: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    """Paths inside ``entry`` that the ``entry`` definition does not declare."""
    walker = _SchemaWalker(schema)
    return walker.undeclared(entry, {"$ref": "#/$defs/entry"})


def _drop_paths(tree: Any, paths: Sequence[tuple[Any, ...]]) -> Any:
    for path in sorted(paths, key=len, reverse=True):
        node = tree
        ok = True
        for part in path[:-1]:
            try:
                node = node[part]
            except (KeyError, IndexError, TypeError):
                ok = False
                break
        if ok and isinstance(node, dict):
            node.pop(path[-1], None)
    return tree


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

def _ordered(mapping: Mapping[str, Any], order: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in order:
        if k in mapping:
            out[k] = mapping[k]
    for k in sorted(k for k in mapping if k not in out):
        out[k] = mapping[k]
    return out


def order_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    e = dict(entry)
    if isinstance(e.get("scope"), Mapping):
        scope = {}
        for dim, c in _ordered(e["scope"], _ORDER_BY_KEY["scope"]).items():
            if isinstance(c, Mapping):
                c = _ordered(c, _CONSTRAINT_ORDER)
                if isinstance(c.get("range"), Mapping):
                    c["range"] = _ordered(c["range"], _ORDER_BY_KEY["range"])
            scope[dim] = c
        e["scope"] = scope
    if isinstance(e.get("provenance"), Mapping):
        e["provenance"] = _ordered(e["provenance"], _ORDER_BY_KEY["provenance"])
    if isinstance(e.get("verification"), Mapping):
        v = _ordered(e["verification"], _ORDER_BY_KEY["verification"])
        if isinstance(v.get("evidence"), list):
            v["evidence"] = [_ordered(x, _EVIDENCE_ORDER) if isinstance(x, Mapping) else x for x in v["evidence"]]
        if isinstance(v.get("verified_against"), Mapping):
            v["verified_against"] = _ordered(v["verified_against"], _ORDER_BY_KEY["verified_against"])
        e["verification"] = v
    if isinstance(e.get("lifecycle"), Mapping):
        lc = _ordered(e["lifecycle"], _ORDER_BY_KEY["lifecycle"])
        if isinstance(lc.get("resolved_by"), Mapping):
            lc["resolved_by"] = _ordered(lc["resolved_by"], _RESOLVED_BY_ORDER)
        e["lifecycle"] = lc
    if isinstance(e.get("conflicts"), list):
        e["conflicts"] = [_ordered(x, _CONFLICT_ORDER) if isinstance(x, Mapping) else x for x in e["conflicts"]]
    if isinstance(e.get("rule"), Mapping):
        e["rule"] = _ordered(e["rule"], _ORDER_BY_KEY["rule"])
    return _ordered(e, _ENTRY_ORDER)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

class ExportRefused(ToolError):
    """The gate refused. ``str(exc)`` lists every reason."""


def _entries_of(doc: Any, source: str) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(doc, Mapping) and isinstance(doc.get("entries"), list):
        kind = doc.get("kind") if isinstance(doc.get("kind"), str) else None
        entries = doc["entries"]
    elif isinstance(doc, list):
        kind, entries = None, doc
    elif isinstance(doc, Mapping) and "uuid" in doc:
        kind, entries = None, [doc]
    else:
        raise ToolError(f"{source}: expected a v2 document, a list of entries, or a single entry mapping")
    bad = [i for i, e in enumerate(entries) if not isinstance(e, Mapping)]
    if bad:
        raise ToolError(f"{source}: entries {bad} are not mappings")
    return [dict(e) for e in entries], kind


def _previous_index(path: str | None) -> dict[str, Mapping[str, Any]]:
    if not path:
        return {}
    entries, _ = _entries_of(load_document(Path(path)), path)
    return {e["uuid"]: e for e in entries if isinstance(e.get("uuid"), str)}


def build_document(
    inputs: Sequence[str],
    *,
    kind: str | None = None,
    layer: str = DEFAULT_LAYER,
    contributor: str | None = None,
    origin_repo: str | None = None,
    submitted_at: str | None = None,
    previous: str | None = None,
    drop_undeclared: bool = False,
    allow: "redact.Allowlist | None" = None,
    today: str | None = None,
    report=None,
) -> dict[str, Any]:
    """Run the gate and return the ordered, ready-to-serialize document.

    Raises ``ExportRefused`` with every reason when the gate refuses.
    """
    report = report or (lambda msg: print(msg, file=sys.stderr))
    schema = load_schema()
    allow = allow or redact.Allowlist()
    today = today or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    prev = _previous_index(previous)

    entries: list[dict[str, Any]] = []
    kinds: set[str] = set()
    for src in inputs:
        found, doc_kind = _entries_of(load_document(Path(src)), src)
        if doc_kind:
            kinds.add(doc_kind)
        for e in found:
            e["__source__"] = src
            entries.append(e)
    if not entries:
        raise ExportRefused("refused: no entries found in the input")

    if kind is None:
        if len(kinds) == 1:
            kind = kinds.pop()
        elif not kinds:
            raise ExportRefused("refused: input carries no 'kind'; pass --kind")
        else:
            raise ExportRefused(f"refused: inputs disagree on kind {sorted(kinds)}; export one kind per document")
    elif kinds and kinds != {kind}:
        raise ExportRefused(f"refused: --kind {kind} but input declares {sorted(kinds)}")

    reasons: list[str] = []
    out_entries: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        src = entry.pop("__source__")
        label = f"{src}: {validate._entry_label(entry, idx)}"

        # 1. whitelist
        extra = undeclared_fields(entry, schema)
        if extra:
            rendered = ", ".join(json_pointer(p) for p in extra)
            if drop_undeclared:
                _drop_paths(entry, extra)
                report(f"{label}: dropped undeclared field(s) {rendered}")
            else:
                reasons.append(
                    f"{label}: undeclared field(s) {rendered} — the schema is the egress "
                    "whitelist; remove them or re-run with --drop-undeclared"
                )
                continue

        # 2. stamp
        prov = dict(entry.get("provenance") or {}) if isinstance(entry.get("provenance"), Mapping) else {}
        # An existing handle is authoritative; the flag fills gaps and replaces
        # the 'anonymous' placeholder only.
        if "contributor" not in prov or (contributor and prov["contributor"] == DEFAULT_CONTRIBUTOR):
            prov["contributor"] = contributor or DEFAULT_CONTRIBUTOR
        if "origin_repo" not in prov:
            if not origin_repo:
                reasons.append(f"{label}: provenance.origin_repo is missing; pass --origin-repo owner/repo")
                continue
            prov["origin_repo"] = origin_repo
        try:
            new_hash = canonical.content_hash(entry)
        except ToolError as exc:
            reasons.append(f"{label}: {exc}")
            continue
        uuid = entry.get("uuid")
        carried = prev.get(uuid) if isinstance(uuid, str) else None
        unchanged = carried is not None and carried.get("content_hash") == new_hash
        if "submitted_at" not in prov:
            if (
                unchanged
                and isinstance(carried.get("provenance"), Mapping)
                and isinstance(carried["provenance"].get("submitted_at"), str)
            ):
                prov["submitted_at"] = carried["provenance"]["submitted_at"]
            else:
                prov["submitted_at"] = submitted_at or today
        if carried is not None and not unchanged:
            # docs/federation.md: a changed claim is a revision, and a revision
            # advances lifecycle.updated_at. Refuse a revision that pretends
            # nothing changed.
            old_lc = carried.get("lifecycle") if isinstance(carried.get("lifecycle"), Mapping) else {}
            new_lc = entry.get("lifecycle") if isinstance(entry.get("lifecycle"), Mapping) else {}
            old_date, new_date = old_lc.get("updated_at"), new_lc.get("updated_at")
            if isinstance(old_date, str) and isinstance(new_date, str) and new_date <= old_date:
                reasons.append(
                    f"{label}: scope/rule changed against --previous (content_hash "
                    f"{carried.get('content_hash')} -> {new_hash}) but lifecycle.updated_at "
                    f"({new_date}) did not advance past {old_date}; a revision must advance "
                    "updated_at (and must not touch verification.last_verified_at)"
                )
                continue
        prov["redaction_profile"] = redact.REDACTION_PROFILE
        entry["provenance"] = prov
        if entry.get("content_hash") not in (None, new_hash):
            report(f"{label}: content_hash recomputed ({entry['content_hash']} -> {new_hash})")
        entry["content_hash"] = new_hash
        out_entries.append(order_entry(entry))

    if reasons:
        raise ExportRefused("refused:\n  " + "\n  ".join(reasons))

    out_entries.sort(key=lambda e: str(e.get("uuid", "")))
    updated = [
        e["lifecycle"]["updated_at"]
        for e in out_entries
        if isinstance(e.get("lifecycle"), Mapping) and isinstance(e["lifecycle"].get("updated_at"), str)
    ]
    doc = _ordered(
        {
            "schema_version": 2,
            "kind": kind,
            "layer": layer,
            "updated_at": max(updated) if updated else today,
            "entries": out_entries,
        },
        _DOC_ORDER,
    )

    # 3. validate
    problems, _ = validate.validate_document(doc, "<export>")
    seen: dict[str, int] = {}
    for i, e in enumerate(out_entries):
        u = e.get("uuid")
        if isinstance(u, str):
            if u in seen:
                problems.append(validate.Problem("<export>", f"entries[{i}].uuid", f"duplicate uuid; also entries[{seen[u]}]"))
            else:
                seen[u] = i
    if problems:
        raise ExportRefused("refused: the assembled document does not validate:\n  " + "\n  ".join(p.render() for p in problems))

    # 4. redact
    findings = redact.scan_tree(doc, allow)
    if findings:
        raise ExportRefused(
            f"refused: redaction ({redact.REDACTION_PROFILE}) found {len(findings)} value(s) that must not "
            "be published (values masked; run tools/redact.py --show-matches locally):\n  "
            + "\n  ".join(f.render() for f in findings)
        )
    return doc


def serialize(doc: Mapping[str, Any]) -> str:
    """Deterministic YAML: fixed key order, no key sorting, unicode kept, wide lines."""
    yaml = require_yaml()
    header = (
        f"# Exported by tools/export.py under redaction profile {redact.REDACTION_PROFILE}.\n"
        "# content_hash is derived; regenerate with tools/ rather than editing it.\n"
    )
    body = yaml.safe_dump(
        doc,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
        indent=2,
    )
    return header + body


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="export.py",
        description="Egress gate: whitelist, stamp, validate, redact and serialize knowledge entries for a PR.",
    )
    parser.add_argument("inputs", nargs="+", help="local knowledge files (v2 documents, entry lists or single entries)")
    parser.add_argument("-o", "--output", type=Path, help="write the document here (default: stdout)")
    parser.add_argument("--kind", help="document kind; required when the input does not declare one")
    parser.add_argument("--layer", choices=("unverified", "verified"), default=DEFAULT_LAYER, help="forks always propose 'unverified' (default)")
    parser.add_argument("--contributor", help="GitHub handle for provenance.contributor (default: existing value, else 'anonymous')")
    parser.add_argument("--origin-repo", help="owner/repo this export comes from; required when entries lack provenance.origin_repo")
    parser.add_argument("--submitted-at", help="YYYY-MM-DD to stamp new revisions with (default: today, UTC)")
    parser.add_argument("--previous", help="a prior export; unchanged entries keep their submitted_at from it")
    parser.add_argument("--drop-undeclared", action="store_true", help="drop undeclared fields instead of refusing")
    parser.add_argument("--allow", action="append", default=[], metavar="TERM", help="redaction allowlist term or 're:<regex>' (local only)")
    parser.add_argument("--allow-file", action="append", default=[], metavar="FILE", help="redaction allowlist file (local only)")
    args = parser.parse_args(argv)

    allow = redact.Allowlist.from_args(args.allow, args.allow_file)
    try:
        doc = build_document(
            args.inputs,
            kind=args.kind,
            layer=args.layer,
            contributor=args.contributor,
            origin_repo=args.origin_repo,
            submitted_at=args.submitted_at,
            previous=args.previous,
            drop_undeclared=args.drop_undeclared,
            allow=allow,
        )
    except ExportRefused as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FINDINGS

    text = serialize(doc)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"export: wrote {len(doc['entries'])} entr{'y' if len(doc['entries']) == 1 else 'ies'} to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
