#!/usr/bin/env python3
"""Dry-run planner: what would a sync of this export do, per entry?

    python3 -m vaws_knowledge.sync.plan --export fork-export.yaml [--corpus corpus/] [--json]

Prints one line per entry with an action and a reason. Changes nothing.
Actions (docs/federation.md, "Idempotency"):

    no-op                same uuid, same content_hash
    new                  uuid unknown here; lands in corpus/unverified/<kind>.yaml
    revision             same uuid in corpus/unverified/, different content_hash;
                         lifecycle.updated_at advances, last_verified_at does not
    duplicate-candidate  unknown uuid whose rule is near-identical to an
                         existing entry; both are reported, nothing is merged
    conflict             cannot be applied mechanically — hash mismatch,
                         kind mismatch, duplicate uuid inside the export, or a
                         revision that targets an entry in corpus/verified/

`compute_plan` is the single source of truth for these decisions; propose.py
only renders and applies what it returns.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import difflib
import pathlib
import re
import sys
from typing import Iterable

from vaws_knowledge.sync._common import (  # noqa: E402
    EXIT_ERROR,
    EXIT_GATE,
    EXIT_OK,
    HASH_RE,
    UUID_RE,
    Corpus,
    Located,
    SyncError,
    canonical_json,
    content_hash,
    gates_summary,
    json_dumps,
    load_corpus,
    load_export,
    repo_root_from,
    run_source_gates,
    today,
)

ACTIONS = ("no-op", "new", "revision", "duplicate-candidate", "conflict")

# Near-duplicate thresholds. Deliberately conservative: a false "duplicate
# candidate" costs a human a glance, a false "new" costs the corpus a
# duplicate that later needs a supersedes edit.
TEXT_RATIO_THRESHOLD = 0.90
FINGERPRINT_JACCARD_THRESHOLD = 0.80


@dataclasses.dataclass
class PlanItem:
    uuid: str
    slug: str
    kind: str
    action: str
    reason: str
    current_hash: str | None = None
    proposed_hash: str | None = None
    current_layer: str | None = None
    target_path: str | None = None
    related: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    entry_after: dict | None = None  # the entry as it would be written

    def to_public(self) -> dict:
        d = dataclasses.asdict(self)
        d.pop("entry_after", None)
        return d


@dataclasses.dataclass
class Plan:
    today: str
    origin_repos: list[str]
    items: list[PlanItem]
    corpus_root: str = ""

    def by_action(self, action: str) -> list[PlanItem]:
        return [i for i in self.items if i.action == action]

    def counts(self) -> dict[str, int]:
        return {a: len(self.by_action(a)) for a in ACTIONS}

    def applicable(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in ("new", "revision")]

    def to_public(self) -> dict:
        return {
            "today": self.today,
            "corpus_root": self.corpus_root,
            "origin_repos": self.origin_repos,
            "counts": self.counts(),
            "items": [i.to_public() for i in self.items],
        }


# --------------------------------------------------------------------------
# near-duplicate detection


def _rule_text(entry: dict) -> str:
    """Comparable text for the entry's body.

    A rule is compared on its prose. A measurement has no prose worth
    comparing — two platform_config snapshots of different SoCs read almost
    identically — so it is compared on subject and quantity identity instead.
    Feeding measurement prose to a text-similarity ratio would report the whole
    hardware catalogue as duplicates of itself.
    """
    if "measurement" in entry and "rule" not in entry:
        return _measurement_key_text(entry)
    rule = entry.get("rule") or {}
    parts = [str(rule.get(k, "")) for k in ("summary", "symptom", "root_cause", "resolution")]
    return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()


def _measurement_key_text(entry: dict) -> str:
    measurement = entry.get("measurement") or {}
    subject = measurement.get("subject") or {}
    method = measurement.get("method") or {}
    quantities = measurement.get("quantities") or []
    parts = [str(subject.get("id", "")), str(method.get("type", ""))]
    parts += sorted(
        f"{q.get('name', '')}/{q.get('basis', '')}/{q.get('unit', '')}={q.get('value', '')}"
        for q in quantities
        if isinstance(q, dict)
    )
    return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()


def _fingerprints(entry: dict) -> set[str]:
    rule = entry.get("rule") or {}
    return {
        re.sub(r"\s+", " ", str(f).strip().lower())
        for f in (rule.get("fingerprints") or [])
        if str(f).strip()
    }


def similarity(a: dict, b: dict) -> dict:
    """Return the similarity measures used for the duplicate-candidate decision."""
    ta, tb = _rule_text(a), _rule_text(b)
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    text_ratio = matcher.ratio() if matcher.quick_ratio() >= TEXT_RATIO_THRESHOLD else 0.0
    fa, fb = _fingerprints(a), _fingerprints(b)
    jaccard = len(fa & fb) / len(fa | fb) if (fa and fb) else 0.0
    exact_rule = _body_only_json(a) == _body_only_json(b)
    return {"text_ratio": round(text_ratio, 3), "fingerprint_jaccard": round(jaccard, 3), "exact_rule": exact_rule}


def _body_only_json(entry: dict) -> str:
    """Canonical JSON of the body alone, with the coordinate blanked out.

    Two entries with different bodies (one rule, one measurement) can never be
    byte-identical here, because the payload key is the body's own name.
    """
    body = "measurement" if ("measurement" in entry and "rule" not in entry) else "rule"
    return canonical_json({body: entry.get(body), "scope": {}})


def is_near_duplicate(measures: dict) -> bool:
    return (
        measures["exact_rule"]
        or measures["text_ratio"] >= TEXT_RATIO_THRESHOLD
        or measures["fingerprint_jaccard"] >= FINGERPRINT_JACCARD_THRESHOLD
    )


def find_near_duplicates(entry: dict, candidates: Iterable[tuple[str, dict]]) -> list[tuple[str, dict]]:
    out = []
    for uid, other in candidates:
        if uid == entry.get("uuid"):
            continue
        m = similarity(entry, other)
        if is_near_duplicate(m):
            out.append((uid, m))
    return out


# --------------------------------------------------------------------------
# entry shaping


def normalize_incoming_status(entry: dict) -> list[str]:
    """Fork-side verification is not main-repo verification.

    A proposal can only land in corpus/unverified/, and `verified`/`stale`
    mean "confirmed here by a non-submitter". The verification record itself
    is preserved so the reviewer can follow the evidence. `confidence: high`
    is not allowed on an unverified claim by the schema, so it is lowered too.
    This touches neither scope nor rule, so content_hash is unaffected.
    """
    notes = []
    status = entry.get("status")
    if status in ("verified", "stale"):
        entry["status"] = "unverified"
        notes.append(f"status {status} -> unverified (fork-side verification does not carry over)")
    if entry.get("confidence") == "high" and entry["status"] not in ("verified", "stale", "resolved"):
        entry["confidence"] = "medium"
        notes.append("confidence high -> medium (high is reserved for reviewed claims)")
    return notes


def shape_new_entry(incoming: dict, day: str) -> tuple[dict, list[str]]:
    entry = copy.deepcopy(incoming)
    notes = normalize_incoming_status(entry)
    entry["content_hash"] = content_hash(entry)
    lifecycle = entry.setdefault("lifecycle", {})
    lifecycle.setdefault("first_seen", day)
    lifecycle.setdefault("updated_at", day)
    return entry, notes


def shape_revision(current: dict, incoming: dict, day: str) -> tuple[dict, list[str]]:
    """Apply a revision to the same uuid.

    The fork owns the claim (scope, rule), the human handle (slug), its
    confidence, and the provenance of *this* revision. The main repo owns
    everything that records what happened to the entry here: status,
    lifecycle decisions (first_seen, supersedes, superseded_by, resolved_by),
    recorded conflicts, and the verification record — in particular
    `verification.last_verified_at`, which a rewording must never advance.
    """
    entry = copy.deepcopy(current)
    notes = []
    entry["scope"] = copy.deepcopy(incoming["scope"])
    # The body is fork-owned and replaced wholesale. A revision may also switch
    # variant (a measurement that was mistakenly filed as a rule), so the old
    # body key is dropped rather than left behind next to the new one.
    entry.pop("rule", None)
    entry.pop("measurement", None)
    if "rule" in incoming:
        entry["rule"] = copy.deepcopy(incoming["rule"])
    if "measurement" in incoming:
        entry["measurement"] = copy.deepcopy(incoming["measurement"])
    entry["slug"] = incoming.get("slug", entry.get("slug"))
    entry["content_hash"] = content_hash(entry)
    if "provenance" in incoming:
        entry["provenance"] = copy.deepcopy(incoming["provenance"])
    if "confidence" in incoming:
        entry["confidence"] = incoming["confidence"]
    if entry.get("confidence") == "high" and entry.get("status") not in ("verified", "stale", "resolved"):
        entry["confidence"] = "medium"
        notes.append("confidence high -> medium (high is reserved for reviewed claims)")
    if "verification" not in entry and "verification" in incoming:
        entry["verification"] = copy.deepcopy(incoming["verification"])
        notes.append("verification record adopted from the fork (corpus copy had none)")
    lifecycle = entry.setdefault("lifecycle", {})
    lifecycle["updated_at"] = day
    lifecycle.setdefault("first_seen", day)
    return entry, notes


# --------------------------------------------------------------------------
# the planner


def compute_plan(exports: list[dict], corpus: Corpus, *, day: str | None = None) -> Plan:
    day = today(day)
    items: list[PlanItem] = []
    origins: list[str] = []

    def rel(path: pathlib.Path) -> str:
        # Paths are reported relative to the corpus parent (`corpus/<layer>/…`)
        # so plans and PR bodies never carry a machine-specific prefix.
        try:
            return str(pathlib.Path(path).resolve().relative_to(corpus.root.resolve().parent))
        except ValueError:
            return str(path)

    # First pass: detect uuids repeated inside the export set itself.
    seen_in_export: dict[str, int] = {}
    for doc in exports:
        for entry in doc["entries"]:
            uid = str(entry.get("uuid"))
            seen_in_export[uid] = seen_in_export.get(uid, 0) + 1

    # Entries accepted as `new` in this plan also take part in duplicate
    # detection for later entries of the same export set.
    accepted_new: list[tuple[str, dict]] = []
    corpus_candidates = [(uid, loc.entry) for uid, loc in corpus.index.items()]

    for doc in exports:
        kind = doc["kind"]
        for incoming in doc["entries"]:
            uid = str(incoming.get("uuid"))
            slug = str(incoming.get("slug", ""))
            origin = (incoming.get("provenance") or {}).get("origin_repo")
            if origin and origin not in origins:
                origins.append(origin)
            item = PlanItem(uuid=uid, slug=slug, kind=kind, action="conflict", reason="")

            if not UUID_RE.match(uid):
                item.reason = f"malformed uuid {uid!r}; identity must be a v4 uuid"
                items.append(item)
                continue
            if seen_in_export[uid] > 1:
                item.reason = f"uuid appears {seen_in_export[uid]} times in the export; one identity, one entry"
                items.append(item)
                continue
            recorded = incoming.get("content_hash")
            recomputed = content_hash(incoming)
            item.proposed_hash = recomputed
            if not isinstance(recorded, str) or not HASH_RE.match(recorded):
                item.reason = f"content_hash {recorded!r} is malformed; regenerate it with tools/"
                items.append(item)
                continue
            if recorded != recomputed:
                item.reason = (
                    f"content_hash mismatch: export records {recorded}, canonicalization gives "
                    f"{recomputed}; regenerate with tools/ rather than by hand"
                )
                items.append(item)
                continue

            located: Located | None = corpus.index.get(uid)
            if located is None:
                dups = find_near_duplicates(incoming, corpus_candidates + accepted_new)
                item.target_path = rel(corpus.target_path("unverified", kind))
                if dups:
                    item.action = "duplicate-candidate"
                    item.related = [d[0] for d in dups]
                    details = "; ".join(
                        f"{d[0]} (text={d[1]['text_ratio']}, fingerprints={d[1]['fingerprint_jaccard']}"
                        f"{', exact rule' if d[1]['exact_rule'] else ''})"
                        for d in dups
                    )
                    item.reason = (
                        "uuid unknown here but the rule is near-identical to " + details
                        + "; reported, not merged — humans decide via lifecycle.supersedes"
                    )
                    entry_after, notes = shape_new_entry(incoming, day)
                    item.entry_after = entry_after
                    item.notes = notes
                    items.append(item)
                    continue
                entry_after, notes = shape_new_entry(incoming, day)
                item.action = "new"
                item.reason = f"uuid unknown here; lands in {item.target_path}"
                item.entry_after = entry_after
                item.notes = notes
                accepted_new.append((uid, incoming))
                items.append(item)
                continue

            item.current_layer = located.layer
            item.current_hash = located.entry.get("content_hash")
            item.target_path = rel(located.path)
            if located.kind != kind:
                item.reason = (
                    f"uuid exists here under kind {located.kind!r} but the export says {kind!r}; "
                    "an identity cannot change document family"
                )
                items.append(item)
                continue
            if item.current_hash == recomputed:
                item.action = "no-op"
                item.reason = "same uuid, same content_hash; nothing to propose"
                items.append(item)
                continue
            if located.layer == "verified":
                item.reason = (
                    "revision targets an entry in corpus/verified/; a fork may not write "
                    "there. The claim changed, so the reviewed copy no longer covers it: "
                    "a maintainer must apply this revision through a review-gated PR"
                )
                entry_after, notes = shape_revision(located.entry, incoming, day)
                item.entry_after = entry_after
                item.notes = notes
                items.append(item)
                continue
            entry_after, notes = shape_revision(located.entry, incoming, day)
            item.action = "revision"
            item.reason = (
                f"same uuid, content_hash {item.current_hash[:19]}… -> {recomputed[:19]}…; "
                f"lifecycle.updated_at -> {day}, verification.last_verified_at unchanged"
            )
            item.entry_after = entry_after
            item.notes = notes
            items.append(item)

    return Plan(today=day, origin_repos=origins, items=items, corpus_root=str(corpus.root))


# --------------------------------------------------------------------------
# rendering


def render_plan(plan: Plan) -> str:
    lines = []
    width = max((len(i.action) for i in plan.items), default=6)
    for item in plan.items:
        lines.append(f"{item.action:<{width}}  {item.uuid}  {item.slug}")
        lines.append(f"{'':<{width}}  {item.reason}")
        for note in item.notes:
            lines.append(f"{'':<{width}}  note: {note}")
    counts = plan.counts()
    lines.append("")
    lines.append("summary: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--export", action="append", required=True, type=pathlib.Path,
                        help="fork export document (repeatable)")
    parser.add_argument("--repo", type=pathlib.Path, default=None,
                        help="vaws-knowledge checkout (default: the one containing sync/)")
    parser.add_argument("--corpus", type=pathlib.Path, default=None,
                        help="corpus directory (default: <repo>/corpus)")
    parser.add_argument("--tools-dir", type=pathlib.Path, default=None,
                        help="optional directory of gate scripts; default is the installed vaws-knowledge CLI")
    parser.add_argument("--today", default=None, help="override the date used for lifecycle.updated_at")


def resolve_dirs(args) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path | None]:
    repo = repo_root_from(args.repo)
    corpus = pathlib.Path(args.corpus).resolve() if args.corpus else repo / "corpus"
    tools = pathlib.Path(args.tools_dir).resolve() if args.tools_dir else None
    return repo, corpus, tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser)
    parser.add_argument("--json", action="store_true", help="print the plan as JSON instead of text")
    parser.add_argument("--skip-gates", action="store_true",
                        help="do not run tools/validate.py and tools/redact.py (dry run only)")
    args = parser.parse_args(argv)

    try:
        _, corpus_dir, tools_dir = resolve_dirs(args)
        exports = [load_export(p) for p in args.export]
        corpus = load_corpus(corpus_dir)
        gates = run_source_gates(tools_dir, args.export, skip=args.skip_gates)
        plan = compute_plan(exports, corpus, day=args.today)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        out = plan.to_public()
        out["gates"] = [dataclasses.asdict(g) for g in gates]
        sys.stdout.write(json_dumps(out))
    else:
        print(gates_summary(gates))
        print()
        print(render_plan(plan))
    return EXIT_OK if all(g.ok for g in gates) else EXIT_GATE


if __name__ == "__main__":
    sys.exit(main())
