"""Exact and near-duplicate detection across the whole corpus.

Exact duplicates are a blocking gate because they are always a mistake:

* the same ``content_hash`` under two different ``uuid``\\ s (the same claim
  exported twice with fresh identities), or
* byte-identical canonical ``rule`` + ``scope`` under two ``uuid``\\ s.

Near duplicates are advisory. They are reported with a score and the fields
that matched, and a human decides whether to merge them with
``lifecycle.supersedes``. The bot never merges, never deletes and never picks
which of the pair survives.

All three entry bodies are handled, and they are compared differently on
purpose. A rule is compared on its prose, because that is what a duplicate
rule duplicates. A measurement has almost no distinguishing prose - a whole
vendor catalogue shares one method description and one summary shape - so it
is compared on *subject identity, quantity identity and coordinate*. A
sourced reference is compared on its citation. Running prose similarity over
measurements, or comparing two empty rule strings, would report unrelated
rows as duplicates of one another. A rule, a measurement and a reference are
never a duplicate pair across bodies: they are different kinds of claim.

Usage::

    python3 -m vaws_knowledge.bot.dedup corpus/ examples/ [--json out.json] [--fail-on exact|near|never]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from vaws_knowledge.bot.corpus import (
    RULE_BODY_FIELDS,
    DependencyError,
    EntryRef,
    LoadResult,
    load_paths,
    ordered_pair,
    repo_root,
)
from vaws_knowledge.bot.policy import PolicyError, load_policy
from vaws_knowledge.bot.similarity import (
    fingerprint_similarity,
    normalize_fingerprints,
    normalize_text,
    text_similarity,
)

GATE_ID = "duplicates"


def _canonical_scope(scope: Mapping[str, Any]) -> str:
    """Stable textual form of a scope, used only for exact-equality tests."""

    def norm(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return {str(k): norm(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
        if isinstance(obj, list):
            return sorted((norm(x) for x in obj), key=json.dumps)
        if isinstance(obj, str):
            return " ".join(obj.split())
        return obj

    return json.dumps(norm(scope), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_rule(ref: EntryRef) -> str:
    rule = ref.rule
    fields = {f: normalize_text(str(rule.get(f, ""))) for f in RULE_BODY_FIELDS}
    fields["avoidance"] = normalize_text(str(rule.get("avoidance", "")))
    fields["fingerprints"] = sorted(normalize_fingerprints(ref.fingerprints))
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _quantity_agreement(a: EntryRef, b: EntryRef) -> tuple[float, list[str], list[str]]:
    """Jaccard over quantity identities, plus the shared and disagreeing ones."""
    qa, qb = a.quantities(), b.quantities()
    keys_a, keys_b = set(qa), set(qb)
    shared = keys_a & keys_b
    union = keys_a | keys_b
    score = round(len(shared) / len(union), 4) if union else 0.0
    disagreeing = sorted(f"{name}/{basis}" for name, basis in shared if qa[(name, basis)] != qb[(name, basis)])
    return score, sorted(f"{name}/{basis}" for name, basis in shared), disagreeing


def _score_measurement_pair(a: EntryRef, b: EntryRef) -> dict[str, Any]:
    """Score two measurement entries on subject + quantities + coordinate.

    Entries about different subjects, or established at different
    coordinates, are not comparable and score zero. That is what keeps a
    sixty-three row hardware catalogue from reading as a pile of duplicates.
    """
    same_subject = bool(a.subject_id) and a.subject_id.lower() == b.subject_id.lower()
    same_scope = _canonical_scope(a.scope) == _canonical_scope(b.scope)
    quantity_score, shared, all_disagreeing = _quantity_agreement(a, b)

    comparable = same_subject and same_scope
    # A different value for the same quantity name is only a disagreement if
    # the two entries are talking about the same thing. Ascend910B4 and
    # Ascend310P3 both declare an `fp16_dense_matmul_peak/theoretical` and the
    # numbers differ by two orders of magnitude; that is the catalogue working,
    # not a contradiction. Reporting it as one turns 63 rows into ~1900
    # findings and buries the pair that matters.
    disagreeing = all_disagreeing if comparable else []

    matched: list[str] = []
    if a.content_hash and a.content_hash == b.content_hash:
        matched.append("content_hash")
    if same_subject:
        matched.append("measurement.subject.id")
    if a.method_type and a.method_type == b.method_type:
        matched.append("measurement.method.type")
    if same_scope:
        matched.append("scope")
    if comparable and shared and not disagreeing:
        matched.append("measurement.quantities")

    exact_reason = None
    if a.content_hash and a.content_hash == b.content_hash and a.uuid != b.uuid:
        exact_reason = "same content_hash under different uuids"
    elif (
        a.uuid != b.uuid
        and same_subject
        and same_scope
        and a.measurement_body_key() == b.measurement_body_key()
    ):
        exact_reason = (
            "identical measurement claim (same subject, method and quantities) at the "
            "same coordinate under different uuids"
        )

    if disagreeing:
        # Not a duplicate, and saying "near duplicate, score 1.0" here would be
        # actively harmful: the two entries agree on every quantity *identity*
        # and disagree on a value, so a reviewer told they are duplicates would
        # merge them and silently drop one of two irreconcilable numbers. The
        # pair belongs to bot/conflicts.py, so it is scored zero and named.
        score = 0.0
    else:
        score = round(quantity_score, 4) if comparable else 0.0
    return {
        "score": score,
        "body": "measurement",
        "body_similarity": 0.0,
        "fingerprint_similarity": 0.0,
        "shared_fingerprints": [],
        "field_scores": {
            "measurement.subject.id": 1.0 if same_subject else 0.0,
            "measurement.quantities": quantity_score,
        },
        "shared_quantities": shared,
        "disagreeing_quantities": disagreeing,
        "comparable": comparable,
        "matched_fields": matched,
        "exact_reason": exact_reason,
        "comparison": (
            (
                "contradiction, not duplication: the same quantity identity is "
                "claimed with a different value or unit at the same coordinate "
                f"({', '.join(disagreeing)}). Reported by the coordinate-conflicts "
                "gate; merging this pair would drop one of two irreconcilable numbers."
                if disagreeing
                else "subject + quantity identity + coordinate"
            )
            if comparable
            else "not comparable: different subject or different coordinate"
        ),
    }


def _score_reference_pair(a: EntryRef, b: EntryRef, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Score two sourced references on citation identity, never on empty rules."""
    shingle = int(policy["duplicates"]["shingle_size"])
    field_threshold = float(policy["duplicates"]["field_match_threshold"])
    sa = a.reference.get("source") if isinstance(a.reference.get("source"), Mapping) else {}
    sb = b.reference.get("source") if isinstance(b.reference.get("source"), Mapping) else {}
    text_a = " ".join(
        str(a.reference.get(k) or "") for k in ("kind", "summary", "text")
    ) + " " + " ".join(str(sa.get(k) or "") for k in ("title", "provider", "url"))
    text_b = " ".join(
        str(b.reference.get(k) or "") for k in ("kind", "summary", "text")
    ) + " " + " ".join(str(sb.get(k) or "") for k in ("title", "provider", "url"))
    body = text_similarity(text_a, text_b, shingle)
    same_url = bool(sa.get("url")) and str(sa.get("url")).strip() == str(sb.get("url") or "").strip()
    same_canonical = _canonical_reference(a) == _canonical_reference(b)
    matched: list[str] = []
    if a.content_hash and a.content_hash == b.content_hash:
        matched.append("content_hash")
    if same_url:
        matched.append("reference.source.url")
    if same_canonical:
        matched.append("reference")
    field_scores = {
        "reference.summary": text_similarity(
            str(a.reference.get("summary") or ""), str(b.reference.get("summary") or ""), shingle
        ),
        "reference.text": text_similarity(
            str(a.reference.get("text") or ""), str(b.reference.get("text") or ""), shingle
        ),
    }
    matched.extend(sorted(k for k, v in field_scores.items() if v >= field_threshold))
    exact_reason = None
    if a.content_hash and a.content_hash == b.content_hash and a.uuid != b.uuid:
        exact_reason = "same content_hash under different uuids"
    elif a.uuid != b.uuid and same_canonical:
        exact_reason = "identical sourced-reference body under different uuids"
    return {
        "score": round(body, 4),
        "body": "reference",
        "body_similarity": body,
        "fingerprint_similarity": 0.0,
        "shared_fingerprints": [],
        "field_scores": field_scores,
        "matched_fields": matched,
        "exact_reason": exact_reason,
        "comparison": "sourced reference citation",
    }


def _canonical_reference(ref: EntryRef) -> str:
    body = ref.reference
    source = body.get("source") if isinstance(body.get("source"), Mapping) else {}
    fields = {
        "kind": normalize_text(str(body.get("kind", ""))),
        "summary": normalize_text(str(body.get("summary", ""))),
        "text": normalize_text(str(body.get("text", ""))),
        "source": {
            "title": normalize_text(str(source.get("title", ""))),
            "provider": normalize_text(str(source.get("provider", ""))),
            "url": normalize_text(str(source.get("url", ""))),
        },
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _score_cross_body_pair(a: EntryRef, b: EntryRef) -> dict[str, Any]:
    """Different body variants are different kinds of claim, never duplicates."""
    return {
        "score": 0.0,
        "body": "mixed",
        "body_similarity": 0.0,
        "fingerprint_similarity": 0.0,
        "shared_fingerprints": [],
        "field_scores": {},
        "matched_fields": (
            ["content_hash"] if a.content_hash and a.content_hash == b.content_hash else []
        ),
        "exact_reason": (
            "same content_hash under different uuids"
            if a.content_hash and a.content_hash == b.content_hash and a.uuid != b.uuid
            else None
        ),
        "comparison": (
            f"not comparable: {a.body} body versus {b.body} body"
        ),
    }


def score_pair(a: EntryRef, b: EntryRef, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Score one pair. Pure function of the two entries and the policy."""
    if a.body != b.body:
        return _score_cross_body_pair(a, b)
    if a.body == "measurement":
        return _score_measurement_pair(a, b)
    if a.body == "reference":
        return _score_reference_pair(a, b, policy)
    dup = policy["duplicates"]
    shingle = int(dup["shingle_size"])
    field_threshold = float(dup["field_match_threshold"])

    field_scores: dict[str, float] = {}
    for f in RULE_BODY_FIELDS:
        field_scores[f"rule.{f}"] = text_similarity(
            str(a.rule.get(f, "")), str(b.rule.get(f, "")), shingle
        )
    body = text_similarity(a.rule_body(), b.rule_body(), shingle)
    fp_score, shared_fps = fingerprint_similarity(a.fingerprints, b.fingerprints)
    has_fps = bool(a.fingerprints or b.fingerprints)
    field_scores["rule.fingerprints"] = fp_score if has_fps else 0.0

    score = round(0.7 * body + 0.3 * fp_score, 4) if has_fps else round(body, 4)

    matched = sorted(k for k, v in field_scores.items() if v >= field_threshold)
    if a.content_hash and a.content_hash == b.content_hash:
        matched.insert(0, "content_hash")
    if _canonical_scope(a.scope) == _canonical_scope(b.scope):
        matched.append("scope")

    exact_reason = None
    if a.content_hash and a.content_hash == b.content_hash and a.uuid != b.uuid:
        exact_reason = "same content_hash under different uuids"
    elif (
        a.uuid != b.uuid
        and _canonical_rule(a) == _canonical_rule(b)
        and _canonical_scope(a.scope) == _canonical_scope(b.scope)
    ):
        exact_reason = "identical canonical rule and scope under different uuids"

    return {
        "score": score,
        "body": "rule",
        "body_similarity": body,
        "fingerprint_similarity": fp_score,
        "shared_fingerprints": list(shared_fps),
        "field_scores": field_scores,
        "matched_fields": matched,
        "exact_reason": exact_reason,
        "comparison": "rule prose + fingerprints",
    }


def find_duplicates(loaded: LoadResult, policy: Mapping[str, Any]) -> dict[str, Any]:
    threshold = float(policy["duplicates"]["near_threshold"])
    measurement_threshold = float(policy["measurements"]["near_threshold"])
    exact: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []
    contradicting: list[dict[str, Any]] = []
    entries = loaded.entries
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = ordered_pair(entries[i], entries[j])
            if a.uuid and a.uuid == b.uuid:
                # Same identity in two places is an integrity problem, not a
                # duplicate claim; bot/integrity.py owns it.
                continue
            scored = score_pair(a, b, policy)
            record = {
                "a": a.describe(),
                "b": b.describe(),
                **scored,
                "action": "human decision required; merge with lifecycle.supersedes "
                "or refine both entries. The bot does not merge.",
            }
            if scored["exact_reason"]:
                record["kind"] = "exact"
                exact.append(record)
            elif scored.get("disagreeing_quantities"):
                # Visible, but never offered as something to merge. The
                # blocking decision is bot/conflicts.py's.
                record["kind"] = "contradicting"
                record["action"] = (
                    "not a duplicate: see the coordinate-conflicts gate, which owns "
                    "this pair and blocks on it. Do not merge."
                )
                contradicting.append(record)
            else:
                # Each body has its own near threshold: they are scores of
                # different things (prose overlap versus quantity-set
                # agreement) and one number cannot mean both.
                limit = measurement_threshold if scored.get("body") == "measurement" else threshold
                if scored["score"] >= limit and scored["score"] > 0:
                    record["kind"] = "near"
                    near.append(record)

    def key(rec: dict[str, Any]) -> tuple:
        return (-rec["score"], rec["a"]["uuid"], rec["b"]["uuid"], rec["a"]["location"])

    exact.sort(key=key)
    near.sort(key=key)
    contradicting.sort(key=key)
    return {
        "gate": GATE_ID,
        "near_threshold": threshold,
        "measurement_near_threshold": measurement_threshold,
        "entries_compared": len(entries),
        "exact": exact,
        "near": near,
        "contradicting": contradicting,
        "counts": {
            "exact": len(exact),
            "near": len(near),
            "contradicting": len(contradicting),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    parser.add_argument("--json", dest="json_out", help="write the report to this file")
    parser.add_argument("--policy", help="alternative policy.yaml")
    parser.add_argument(
        "--fail-on",
        choices=("exact", "near", "never"),
        default="exact",
        help="exit non-zero when this class of duplicate is found (default: exact)",
    )
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        root = repo_root()
        loaded = load_paths(args.paths, root)
    except (DependencyError, PolicyError) as exc:
        print(f"bot/dedup: {exc}", file=sys.stderr)
        return 2
    if loaded.errors:
        for err in loaded.errors:
            print(f"bot/dedup: load error: {err.path}: {err.message}", file=sys.stderr)
        return 2
    report = find_duplicates(loaded, policy)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    failing = report["counts"]["exact"] > 0
    if args.fail_on == "near":
        failing = failing or report["counts"]["near"] > 0
    elif args.fail_on == "never":
        failing = False
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
