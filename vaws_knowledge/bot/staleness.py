"""Staleness sweep (docs/lifecycle.md, "Staleness").

Compares ``verification.last_verified_at`` and
``verification.verified_against`` of every ``verified`` entry against the
policy in ``bot/policy.yaml`` and **proposes** ``verified -> stale``
downgrades. It writes nothing into the corpus: the output is a JSON proposal
list that a maintainer turns into a reviewed PR. ``corpus/verified/`` is
review-gated, and the sweep running with write access would make the bot the
author of a trust decision.

Reasons a proposal is raised:

* ``horizon_exceeded`` — ``last_verified_at`` is older than
  ``reverification_horizon_days`` relative to ``--as-of``,
* ``out_of_window:<dimension>`` — ``verified_against.<dimension>`` falls outside
  the supported window for that dimension,
* ``retired_soc`` — ``verified_against.soc`` is in ``retired_soc``,
* ``unorderable:<dimension>`` — the recorded version cannot be compared with a
  bounded window (reported, not proposed, because the bot cannot tell).

Usage::

    python3 bot/staleness.py corpus/ [--as-of YYYY-MM-DD] [--json out.json]
                                     [--fail-on-proposals]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from vaws_knowledge.bot import versions
from vaws_knowledge.bot.corpus import DependencyError, EntryRef, LoadResult, load_paths, repo_root
from vaws_knowledge.bot.policy import PolicyError, load_policy

GATE_ID = "staleness"


def parse_date(text: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def evaluate_entry(
    ref: EntryRef, policy: Mapping[str, Any], as_of: _dt.date
) -> dict[str, Any]:
    st = policy["staleness"]
    horizon = int(st["reverification_horizon_days"])
    window: Mapping[str, Any] = st["supported_window"]
    retired = {str(s).strip().lower() for s in st["retired_soc"]}

    reasons: list[str] = []
    notes: list[str] = []
    verification = ref.verification
    against = verification.get("verified_against")
    against = against if isinstance(against, Mapping) else {}

    last = parse_date(verification.get("last_verified_at"))
    age_days: Optional[int] = None
    if last is None:
        reasons.append("last_verified_at_missing_or_invalid")
    else:
        age_days = (as_of - last).days
        if age_days > horizon:
            reasons.append("horizon_exceeded")

    for dim in sorted(window):
        bounds = window[dim]
        if bounds.get("min") is None and bounds.get("max") is None:
            continue  # not enforced
        observed = against.get(dim)
        if observed in (None, ""):
            notes.append(f"unorderable:{dim} (verified_against.{dim} missing)")
            continue
        try:
            inside = versions.within(
                str(observed), bounds.get("min"), bounds.get("max"), dimension=dim
            )
        except versions.RangeNotAllowed as exc:
            # A supported-version window on an exact-match-only dimension is a
            # policy mistake, not an unorderable value. Say which, rather than
            # letting it read as "this entry could not be checked".
            notes.append(f"policy_error:{dim} ({exc})")
            continue
        if inside is None:
            notes.append(f"unorderable:{dim}")
        elif not inside:
            reasons.append(f"out_of_window:{dim}")

    soc = str(against.get("soc", "")).strip().lower()
    if soc and soc in retired:
        reasons.append("retired_soc")

    return {
        "entry": ref.describe(),
        "last_verified_at": verification.get("last_verified_at"),
        "age_days": age_days,
        "reasons": reasons,
        "notes": notes,
    }


def sweep(loaded: LoadResult, policy: Mapping[str, Any], as_of: _dt.date) -> dict[str, Any]:
    downgrade_from = {str(s) for s in policy["staleness"]["downgrade_from"]}
    proposals: list[dict[str, Any]] = []
    already_stale: list[dict[str, Any]] = []
    unorderable: list[dict[str, Any]] = []
    considered = 0

    for ref in loaded.entries:
        if ref.status not in downgrade_from and ref.status != "stale":
            continue
        considered += 1
        result = evaluate_entry(ref, policy, as_of)
        if result["notes"]:
            unorderable.append({"entry": ref.describe(), "notes": result["notes"]})
        if ref.status == "stale":
            already_stale.append(result)
            continue
        if result["reasons"]:
            proposals.append(
                {
                    **result,
                    "current_status": ref.status,
                    "proposed_status": "stale",
                    "action": "open a reviewed PR that sets status: stale and advances "
                    "lifecycle.updated_at only; do not touch last_verified_at",
                }
            )

    key = lambda r: (r["entry"]["uuid"], r["entry"]["location"])  # noqa: E731
    proposals.sort(key=key)
    already_stale.sort(key=key)
    unorderable.sort(key=key)
    return {
        "gate": GATE_ID,
        "as_of": as_of.isoformat(),
        "policy": {
            "reverification_horizon_days": policy["staleness"]["reverification_horizon_days"],
            "enforced_window_dimensions": sorted(
                d
                for d, b in policy["staleness"]["supported_window"].items()
                if b.get("min") is not None or b.get("max") is not None
            ),
            "retired_soc": sorted(str(s) for s in policy["staleness"]["retired_soc"]),
        },
        "entries_considered": considered,
        "proposals": proposals,
        "already_stale": already_stale,
        "unorderable": unorderable,
        "counts": {
            "proposals": len(proposals),
            "already_stale": len(already_stale),
            "unorderable": len(unorderable),
        },
        "policy_statement": (
            "Proposals only. This sweep never edits corpus/verified/; downgrades land "
            "through a reviewed PR and promotion back needs new evidence."
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--policy")
    parser.add_argument("--as-of", help="evaluation date, YYYY-MM-DD (default: today UTC)")
    parser.add_argument(
        "--fail-on-proposals",
        action="store_true",
        help="exit 1 when at least one downgrade is proposed (for scheduled audits)",
    )
    args = parser.parse_args(argv)
    as_of = parse_date(args.as_of) if args.as_of else today_utc()
    if as_of is None:
        print("bot/staleness: --as-of must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        policy = load_policy(args.policy)
        loaded = load_paths(args.paths, repo_root())
    except (DependencyError, PolicyError) as exc:
        print(f"bot/staleness: {exc}", file=sys.stderr)
        return 2
    if loaded.errors:
        for err in loaded.errors:
            print(f"bot/staleness: load error: {err.path}: {err.message}", file=sys.stderr)
        return 2
    report = sweep(loaded, policy, as_of)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    if args.fail_on_proposals and report["counts"]["proposals"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
