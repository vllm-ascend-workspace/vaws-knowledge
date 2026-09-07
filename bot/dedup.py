"""Exact and near-duplicate detection across the whole corpus.

Exact duplicates are a blocking gate because they are always a mistake:

* the same ``content_hash`` under two different ``uuid``\\ s (the same claim
  exported twice with fresh identities), or
* byte-identical canonical ``rule`` + ``scope`` under two ``uuid``\\ s.

Near duplicates are advisory. They are reported with a score and the fields
that matched, and a human decides whether to merge them with
``lifecycle.supersedes``. The bot never merges, never deletes and never picks
which of the pair survives.

Usage::

    python3 bot/dedup.py corpus/ examples/ [--json out.json] [--fail-on exact|near|never]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.corpus import (
    RULE_BODY_FIELDS,
    DependencyError,
    EntryRef,
    LoadResult,
    load_paths,
    ordered_pair,
    repo_root,
)
from bot.policy import PolicyError, load_policy
from bot.similarity import (
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


def score_pair(a: EntryRef, b: EntryRef, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Score one pair. Pure function of the two entries and the policy."""
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
        "body_similarity": body,
        "fingerprint_similarity": fp_score,
        "shared_fingerprints": list(shared_fps),
        "field_scores": field_scores,
        "matched_fields": matched,
        "exact_reason": exact_reason,
    }


def find_duplicates(loaded: LoadResult, policy: Mapping[str, Any]) -> dict[str, Any]:
    threshold = float(policy["duplicates"]["near_threshold"])
    exact: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []
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
            elif scored["score"] >= threshold:
                record["kind"] = "near"
                near.append(record)

    def key(rec: dict[str, Any]) -> tuple:
        return (-rec["score"], rec["a"]["uuid"], rec["b"]["uuid"], rec["a"]["location"])

    exact.sort(key=key)
    near.sort(key=key)
    return {
        "gate": GATE_ID,
        "near_threshold": threshold,
        "entries_compared": len(entries),
        "exact": exact,
        "near": near,
        "counts": {"exact": len(exact), "near": len(near)},
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
