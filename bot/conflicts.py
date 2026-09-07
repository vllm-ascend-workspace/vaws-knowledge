"""Coordinate-conflict detection.

Two entries conflict when their ``scope`` coordinates overlap, they describe
the same phenomenon, and their ``rule.root_cause`` / ``rule.resolution``
diverge. The doctrine (README, "Conflicts are incomplete coordinates") says
this is almost never a factual dispute: it is a dimension nobody bounded. So
this module:

* diffs the two coordinates dimension by dimension,
* derives ``undeclared_dimensions`` — dimensions where either side claims
  ``any`` (or an unbounded range), and dimensions whose bounds cannot be
  compared,
* emits a schema-shaped ``conflicts`` record for **both** entries,
* never selects a winner and never drops either entry.

Phenomenon and divergence are judged by dependency-free text similarity
(thresholds in ``bot/policy.yaml``). Because a heuristic will miss some
contradictions, the gate also accepts *asserted* pairs (``--asserted``) from
a human or an LLM triage step. An assertion only says "these two look
contradictory"; the coordinate diff and the blocking decision stay here, in
deterministic code.

A recorded conflict counts as *resolved* when every dimension it lists is now
bounded and comparable on both entries, or when one side left the live corpus
(``deprecated`` / ``superseded_by``). Unresolved conflicts involving an entry
that is, or is proposed to be, ``verified`` block the gate.

Usage::

    python3 bot/conflicts.py corpus/ examples/ [--json out.json] [--asserted pairs.json]
                                               [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import versions
from bot.corpus import (
    SCOPE_DIMENSIONS,
    DependencyError,
    EntryRef,
    LoadResult,
    constraint_kind,
    load_paths,
    normalized_values,
    ordered_pair,
    repo_root,
)
from bot.policy import PolicyError, load_policy
from bot.similarity import fingerprint_similarity, text_similarity

GATE_ID = "coordinate-conflicts"

LIVE_STATUSES = frozenset({"verified", "unverified", "stale"})
PROMOTION_STATUSES = frozenset({"verified", "stale"})


# ---------------------------------------------------------------------------
# per-dimension comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionRelation:
    dimension: str
    overlap: str  # "overlap" | "disjoint" | "unknown"
    undeclared: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "overlap": self.overlap,
            "undeclared": self.undeclared,
            "reason": self.reason,
        }


def _is_fully_unbounded(constraint: Mapping[str, Any]) -> bool:
    rng = constraint.get("range", {})
    return rng.get("min") is None and rng.get("max") is None


def relate_dimension(dim: str, ca: Any, cb: Any) -> DimensionRelation:
    """Compare one scope dimension across two entries."""
    ka, kb = constraint_kind(ca), constraint_kind(cb)

    if "invalid" in (ka, kb):
        return DimensionRelation(dim, "unknown", True, "malformed constraint on at least one side")
    if ka == "any" or kb == "any":
        sides = [s for s, k in (("a", ka), ("b", kb)) if k == "any"]
        return DimensionRelation(dim, "overlap", True, f"claimed any by {'+'.join(sides)}")
    if (ka == "range" and _is_fully_unbounded(ca)) or (kb == "range" and _is_fully_unbounded(cb)):
        return DimensionRelation(
            dim, "overlap", True, "range with both bounds null is an unstated any"
        )

    if ka == "values" and kb == "values":
        va, vb = normalized_values(ca), normalized_values(cb)
        if va & vb:
            reason = "identical value sets" if va == vb else "value sets intersect"
            return DimensionRelation(dim, "overlap", False, reason)
        return DimensionRelation(dim, "disjoint", False, "value sets are disjoint")

    if ka == "range" and kb == "range":
        return _relate_ranges(dim, ca["range"], cb["range"])

    # mixed: values on one side, range on the other
    values_c, range_c = (ca, cb) if ka == "values" else (cb, ca)
    return _relate_values_to_range(dim, values_c, range_c)


def _relate_ranges(dim: str, ra: Mapping[str, Any], rb: Mapping[str, Any]) -> DimensionRelation:
    a_min = versions.bound_or_inf(ra.get("min"), "min")
    a_max = versions.bound_or_inf(ra.get("max"), "max")
    b_min = versions.bound_or_inf(rb.get("min"), "min")
    b_max = versions.bound_or_inf(rb.get("max"), "max")
    # No None check on the bounds: bound_or_inf yields the value or an
    # unbounded sentinel, never None. Unorderable bounds surface below, as an
    # undecidable comparison, which is where the ordering rules live.
    try:
        c1 = versions.compare(a_min, b_max, dimension=dim)
        c2 = versions.compare(b_min, a_max, dimension=dim)
    except versions.RangeNotAllowed as exc:
        # Malformed rather than ambiguous. Reported loudly instead of raised,
        # so one bad entry cannot take the whole gate down, and marked
        # undeclared so it cannot be promoted while it stands.
        return DimensionRelation(dim, "unknown", True, f"contract violation: {exc}")
    if c1 is None or c2 is None:
        return DimensionRelation(dim, "unknown", True, "range bounds cannot be ordered")
    if c1 <= 0 and c2 <= 0:
        same = (
            versions.compare(a_min, b_min, dimension=dim) == 0
            and versions.compare(a_max, b_max, dimension=dim) == 0
        )
        return DimensionRelation(
            dim, "overlap", False, "identical ranges" if same else "ranges intersect"
        )
    return DimensionRelation(dim, "disjoint", False, "ranges do not intersect")


def _relate_values_to_range(
    dim: str, values_c: Mapping[str, Any], range_c: Mapping[str, Any]
) -> DimensionRelation:
    rng = range_c["range"]
    inside = outside = undecidable = 0
    for value in sorted(normalized_values(values_c)):
        try:
            result = versions.within(
                value, rng.get("min"), rng.get("max"), dimension=dim
            )
        except versions.RangeNotAllowed as exc:
            return DimensionRelation(dim, "unknown", True, f"contract violation: {exc}")
        if result is None:
            undecidable += 1
        elif result:
            inside += 1
        else:
            outside += 1
    if inside:
        return DimensionRelation(dim, "overlap", False, "at least one value lies inside the range")
    if undecidable:
        return DimensionRelation(
            dim, "unknown", True, "values and range bounds are not mutually orderable"
        )
    return DimensionRelation(dim, "disjoint", False, "all values lie outside the range")


# ---------------------------------------------------------------------------
# coordinate diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinateDiff:
    relations: tuple[DimensionRelation, ...]

    @property
    def overlaps(self) -> bool:
        return all(r.overlap != "disjoint" for r in self.relations)

    @property
    def undeclared_dimensions(self) -> list[str]:
        return [r.dimension for r in self.relations if r.undeclared]

    @property
    def distinguishing_dimensions(self) -> list[str]:
        """Bounded on both sides, comparable, overlapping but not identical."""
        return [
            r.dimension
            for r in self.relations
            if not r.undeclared
            and r.overlap == "overlap"
            and r.reason in ("value sets intersect", "ranges intersect")
        ]

    @property
    def disjoint_dimensions(self) -> list[str]:
        return [r.dimension for r in self.relations if r.overlap == "disjoint"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "overlaps": self.overlaps,
            "undeclared_dimensions": self.undeclared_dimensions,
            "distinguishing_dimensions": self.distinguishing_dimensions,
            "disjoint_dimensions": self.disjoint_dimensions,
            "dimensions": [r.as_dict() for r in self.relations],
        }


def diff_coordinates(a: EntryRef, b: EntryRef) -> CoordinateDiff:
    relations = tuple(
        relate_dimension(dim, a.scope.get(dim), b.scope.get(dim)) for dim in SCOPE_DIMENSIONS
    )
    return CoordinateDiff(relations)


# ---------------------------------------------------------------------------
# contradiction heuristics
# ---------------------------------------------------------------------------


def phenomenon_similarity(a: EntryRef, b: EntryRef) -> tuple[float, bool]:
    fp_score, shared = fingerprint_similarity(a.fingerprints, b.fingerprints)
    symptom = text_similarity(str(a.rule.get("symptom", "")), str(b.rule.get("symptom", "")))
    if a.fingerprints or b.fingerprints:
        score = round(0.5 * symptom + 0.5 * fp_score, 4)
    else:
        score = symptom
    return score, bool(shared)


def explanation_similarity(a: EntryRef, b: EntryRef) -> float:
    ea = f"{a.rule.get('root_cause', '')}\n{a.rule.get('resolution', '')}"
    eb = f"{b.rule.get('root_cause', '')}\n{b.rule.get('resolution', '')}"
    return text_similarity(ea, eb)


def _linked_by_lifecycle(a: EntryRef, b: EntryRef) -> bool:
    """Supersession already reconciles the pair; it is not a conflict."""
    for x, y in ((a, b), (b, a)):
        lc = x.lifecycle
        if lc.get("superseded_by") == y.uuid:
            return True
        sup = lc.get("supersedes")
        if isinstance(sup, list) and y.uuid in sup:
            return True
    return False


def _recorded_against(a: EntryRef, b: EntryRef) -> bool:
    return any(c.get("with") == b.uuid for c in a.conflicts) or any(
        c.get("with") == a.uuid for c in b.conflicts
    )


# ---------------------------------------------------------------------------
# conflict records
# ---------------------------------------------------------------------------


def build_note(other: EntryRef, diff: CoordinateDiff) -> str:
    undeclared = ", ".join(diff.undeclared_dimensions) or "none"
    distinguishing = ", ".join(diff.distinguishing_dimensions) or "none"
    return (
        f"Coordinate overlap with {other.uuid} ({other.slug}) with contradictory "
        f"root_cause/resolution. Undeclared or non-comparable dimensions: {undeclared}. "
        f"Bounded-but-different dimensions: {distinguishing}. Refine the coordinate on "
        f"both entries; the pipeline does not arbitrate which is correct."
    )


def make_records(
    a: EntryRef, b: EntryRef, diff: CoordinateDiff, recorded_at: str
) -> list[dict[str, Any]]:
    """Schema-shaped ``conflict`` objects, one per side, symmetric."""
    records = []
    for me, other in ((a, b), (b, a)):
        records.append(
            {
                "entry_uuid": me.uuid,
                "entry_location": me.location,
                "conflict": {
                    "with": other.uuid,
                    "undeclared_dimensions": list(diff.undeclared_dimensions),
                    "recorded_at": recorded_at,
                    "note": build_note(other, diff),
                },
            }
        )
    return records


def resolution_state(
    owner: EntryRef, record: Mapping[str, Any], other: Optional[EntryRef]
) -> tuple[str, str]:
    """Classify an existing ``conflicts[]`` record on ``owner``.

    Returns ``(state, reason)`` with state in ``resolved`` / ``unresolved`` /
    ``dangling``.
    """
    if other is None:
        return "dangling", "counterpart uuid is not in the scanned corpus"
    if owner.status in ("deprecated", "resolved") or other.status in ("deprecated", "resolved"):
        return "resolved", "one side is no longer a live claim"
    if owner.lifecycle.get("superseded_by") or other.lifecycle.get("superseded_by"):
        return "resolved", "one side has been superseded"
    listed = record.get("undeclared_dimensions")
    if not isinstance(listed, list) or not listed:
        return "unresolved", "record lists no dimensions to refine"
    diff = diff_coordinates(owner, other)
    still = sorted(set(listed) & set(diff.undeclared_dimensions))
    if still:
        return "unresolved", "still undeclared on at least one side: " + ", ".join(still)
    if not diff.overlaps:
        return "resolved", "coordinates no longer overlap after refinement"
    return "unresolved", (
        "all listed dimensions are now bounded but the coordinates still overlap; "
        "human review required"
    )


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def _blocking_reason(a: EntryRef, b: EntryRef) -> Optional[str]:
    """A conflict blocks when either side is at or beyond promotion."""
    for ref in (a, b):
        if ref.status in PROMOTION_STATUSES or ref.in_verified_dir:
            return f"{ref.uuid} is {ref.status or 'unknown'} in {ref.location}"
    return None


def load_asserted_pairs(path: str | Path | None) -> set[tuple[str, str]]:
    """Read ``[{"a": uuid, "b": uuid}, ...]`` or ``[[uuid, uuid], ...]``."""
    if not path:
        return set()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, Mapping):
        raw = raw.get("pairs", [])
    pairs: set[tuple[str, str]] = set()
    for item in raw:
        if isinstance(item, Mapping):
            x, y = str(item.get("a", "")), str(item.get("b", ""))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            x, y = str(item[0]), str(item[1])
        else:
            raise ValueError("asserted pairs must be {a,b} objects or two-item lists")
        if not x or not y or x == y:
            raise ValueError(f"asserted pair is not two distinct uuids: {item!r}")
        pairs.add((min(x, y), max(x, y)))
    return pairs


def find_conflicts(
    loaded: LoadResult,
    policy: Mapping[str, Any],
    as_of: str,
    asserted: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    cfg = policy["conflicts"]
    same_thr = float(cfg["same_phenomenon_threshold"])
    diverge_max = float(cfg["divergent_explanation_max"])
    asserted_set = {(min(x, y), max(x, y)) for x, y in asserted}

    by_uuid = loaded.by_uuid()
    entries = [e for e in loaded.entries if e.uuid]

    detected: list[dict[str, Any]] = []
    unattributable: list[dict[str, Any]] = []
    already_recorded: list[dict[str, Any]] = []

    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = ordered_pair(entries[i], entries[j])
            if a.uuid == b.uuid or _linked_by_lifecycle(a, b):
                continue
            key = (min(a.uuid, b.uuid), max(a.uuid, b.uuid))
            asserted_here = key in asserted_set
            phen, shared_fp = phenomenon_similarity(a, b)
            expl = explanation_similarity(a, b)
            heuristic = (phen >= same_thr or shared_fp) and expl <= diverge_max
            if not (heuristic or asserted_here):
                continue
            diff = diff_coordinates(a, b)
            record = {
                "a": a.describe(),
                "b": b.describe(),
                "source": "asserted" if asserted_here else "heuristic",
                "signals": {
                    "phenomenon_similarity": phen,
                    "shared_fingerprint": shared_fp,
                    "explanation_similarity": expl,
                },
                "coordinate_diff": diff.as_dict(),
            }
            if not diff.overlaps:
                record["outcome"] = "not_a_conflict"
                record["reason"] = (
                    "coordinates are disjoint on: " + ", ".join(diff.disjoint_dimensions)
                )
                if asserted_here:
                    # Somebody asserted a contradiction the coordinates already
                    # explain; worth showing, never worth blocking.
                    unattributable.append(record)
                continue
            if _recorded_against(a, b):
                record["outcome"] = "already_recorded"
                already_recorded.append(record)
                continue
            blocking = _blocking_reason(a, b)
            if not diff.undeclared_dimensions:
                record["outcome"] = "unattributable"
                record["reason"] = (
                    "coordinates overlap and every dimension is bounded and comparable on "
                    "both sides; the schema's conflict record requires at least one "
                    "undeclared dimension, so this needs human review"
                )
                record["blocking"] = blocking is not None
                record["blocking_reason"] = blocking
                unattributable.append(record)
                continue
            record["outcome"] = "conflict"
            record["undeclared_dimensions"] = diff.undeclared_dimensions
            record["records"] = make_records(a, b, diff, as_of)
            record["blocking"] = blocking is not None
            record["blocking_reason"] = blocking
            detected.append(record)

    existing: list[dict[str, Any]] = []
    for ref in entries:
        for idx, rec in enumerate(ref.conflicts):
            other_uuid = str(rec.get("with", ""))
            counterparts = by_uuid.get(other_uuid, [])
            other = counterparts[0] if counterparts else None
            state, reason = resolution_state(ref, rec, other)
            blocking = (
                state != "resolved"
                and (ref.status in PROMOTION_STATUSES or ref.in_verified_dir)
            )
            existing.append(
                {
                    "entry": ref.describe(),
                    "record_index": idx,
                    "with": other_uuid,
                    "state": state,
                    "reason": reason,
                    "blocking": blocking,
                }
            )

    def pair_sort(rec: dict[str, Any]) -> tuple:
        return (rec["a"]["uuid"], rec["b"]["uuid"], rec["a"]["location"], rec["b"]["location"])

    detected.sort(key=pair_sort)
    unattributable.sort(key=pair_sort)
    already_recorded.sort(key=pair_sort)
    existing.sort(key=lambda r: (r["entry"]["uuid"], r["record_index"], r["entry"]["location"]))

    blocking_count = (
        sum(1 for r in detected if r["blocking"])
        + sum(1 for r in unattributable if r.get("blocking"))
        + sum(1 for r in existing if r["blocking"])
    )
    return {
        "gate": GATE_ID,
        "as_of": as_of,
        "thresholds": {
            "same_phenomenon_threshold": same_thr,
            "divergent_explanation_max": diverge_max,
        },
        "entries_compared": len(entries),
        "asserted_pairs": sorted(asserted_set),
        "conflicts": detected,
        "unattributable": unattributable,
        "already_recorded": already_recorded,
        "existing_records": existing,
        "counts": {
            "conflicts": len(detected),
            "unattributable": len(unattributable),
            "already_recorded": len(already_recorded),
            "existing_unresolved": sum(1 for r in existing if r["state"] != "resolved"),
            "blocking": blocking_count,
        },
        "policy_statement": (
            "The bot records undeclared dimensions on both entries and never selects "
            "a winner. Unresolved conflicts block promotion to verified."
        ),
    }


def today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--policy")
    parser.add_argument("--asserted", help="JSON file of asserted contradictory uuid pairs")
    parser.add_argument("--as-of", default=None, help="recorded_at date, YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        loaded = load_paths(args.paths, repo_root())
        asserted = load_asserted_pairs(args.asserted)
    except (DependencyError, PolicyError, ValueError, OSError) as exc:
        print(f"bot/conflicts: {exc}", file=sys.stderr)
        return 2
    if loaded.errors:
        for err in loaded.errors:
            print(f"bot/conflicts: load error: {err.path}: {err.message}", file=sys.stderr)
        return 2
    report = find_conflicts(loaded, policy, args.as_of or today_utc(), asserted)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 1 if report["counts"]["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
