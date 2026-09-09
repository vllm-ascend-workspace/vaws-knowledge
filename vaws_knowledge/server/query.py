"""Retrieval across the three layers, with coordinate matching.

The reason schemas/knowledge-v2.schema.json forces all twelve `scope`
dimensions is so that a reader can mechanically decide whether an entry
applies to their own build. This module is where that decision is made, and
the rule it follows is:

    an entry must never come back looking as though it applied when it did not.

So each dimension gets an explicit verdict, and the verdict travels with the
result:

    covered      the entry bounds the dimension and the reader's value is
                 inside those bounds
    assumed_any  the entry claims independence (`any` + `basis`); it matches,
                 but on an unproven claim, and the basis is reported
    unchecked    the reader supplied no value for this dimension, so nothing
                 was verified about it
    undecidable  the entry bounds the dimension with a range, but the values
                 involved cannot be ordered (e.g. a non-numeric build string)
    mismatch     the reader's value is outside the entry's bounds

An entry with any `mismatch` does not apply. It is dropped from the result
set by default, and when explicitly requested it comes back with
``applies: false`` and the offending dimensions named. A fact established on
one SoC quietly reused on another is the confusion this repo exists to remove.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from vaws_knowledge import package_version

from .layers import (
    DEFAULT_STATUSES,
    ENV_CORPUS,
    LAYER_PRECEDENCE,
    LAYERS,
    OPT_IN_STATUSES,
    LoadedEntry,
    LoadReport,
    ServiceConfig,
    load_config,
    load_entries,
    shared_source,
)

#: All twelve dimensions of the applicability coordinate.
SCOPE_DIMENSIONS: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "python_abi",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
    "model",
    "topology",
    "execution_mode",
    "component",
)

#: The dimensions a reader is normally able to state about their own build.
#: `python_abi` and `component` are accepted too, but a reader rarely knows
#: which subsystem their symptom belongs to, so they are not expected.
READER_DIMENSIONS: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
    "model",
    "topology",
    "execution_mode",
)

COVERED = "covered"
ASSUMED_ANY = "assumed_any"
UNCHECKED = "unchecked"
UNDECIDABLE = "undecidable"
MISMATCH = "mismatch"

STATUS_RANK = {"verified": 0, "stale": 1, "resolved": 1, "unverified": 2, "deprecated": 3}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

#: docs/lifecycle.md: "They are returned labelled, so a consumer knows not to
#: trust the version bounds."
STALE_WARNING = (
    "status=stale: this entry was verified once, but not re-verified against a "
    "recent enough environment. Do not trust its version bounds; the diagnosis "
    "is usually still the fastest route to a root cause."
)

NO_RESULT_MEANING = (
    "No entry matched. That means UNKNOWN, not supported and not absent-therefore-fine. "
    "Treat a missing fact as unexamined."
)

_TOKEN_RE = re.compile(r"[a-z0-9_.+/-]{2,}")
_WS_RE = re.compile(r"\s+")

_FIELD_WEIGHTS = {
    "summary": 3.0,
    "symptom": 2.5,
    "root_cause": 2.0,
    "resolution": 1.5,
    "avoidance": 1.0,
}
_FINGERPRINT_WEIGHT = 4.0
_SLUG_WEIGHT = 2.0


# --------------------------------------------------------------------------
# version ordering
# --------------------------------------------------------------------------


def _version_key(value: str) -> tuple[tuple[int, int, str], ...] | None:
    """Order-comparable key for a version-ish string, or None if unorderable.

    Build metadata after '+' is dropped: `0.0.0+example` and `0.0.0+abc` are
    the same point in the ordering. Alphabetic segments sort below numeric
    ones at the same position, so `8.1.RC1` < `8.1.0`, which is the intent of
    a release-candidate label.

    Returning None matters: it is what produces an `undecidable` verdict
    instead of a silent match or a silent drop.
    """

    text = str(value).strip()
    if not text:
        return None
    text = text.split("+", 1)[0]
    tokens = [tok for tok in re.split(r"[^0-9A-Za-z]+", text) if tok]
    if not tokens:
        return None
    key: list[tuple[int, int, str]] = []
    saw_number = False
    for tok in tokens:
        if tok.isdigit():
            saw_number = True
            key.append((1, int(tok), ""))
        else:
            key.append((0, 0, tok.lower()))
    if not saw_number:
        # Pure words ("unknown", "latest") are not a version.
        return None
    return tuple(key)


def _compare_versions(left: str, right: str) -> int | None:
    lk, rk = _version_key(left), _version_key(right)
    if lk is None or rk is None:
        return None
    return (lk > rk) - (lk < rk)


def _norm(value: Any) -> str:
    return _WS_RE.sub(" ", str(value)).strip()


# --------------------------------------------------------------------------
# coordinate matching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionVerdict:
    dimension: str
    verdict: str
    constraint: str
    reader_value: str | None = None
    basis: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "dimension": self.dimension,
            "verdict": self.verdict,
            "entry_constraint": self.constraint,
            "reader_value": self.reader_value,
        }
        if self.basis:
            out["independence_basis"] = self.basis
        if self.detail:
            out["detail"] = self.detail
        return out


def _render_constraint(constraint: Any) -> str:
    if not isinstance(constraint, Mapping):
        return "malformed"
    if constraint.get("any") is True:
        return "any"
    if isinstance(constraint.get("values"), (list, tuple)):
        return "values[" + ",".join(str(v) for v in constraint["values"]) + "]"
    rng = constraint.get("range")
    if isinstance(rng, Mapping):
        low = rng.get("min")
        high = rng.get("max")
        return f"range[{'-inf' if low is None else low}..{'+inf' if high is None else high}]"
    return "malformed"


def evaluate_dimension(dimension: str, constraint: Any, reader_value: Any) -> DimensionVerdict:
    rendered = _render_constraint(constraint)
    reader = _norm(reader_value) if reader_value not in (None, "") else None

    if not isinstance(constraint, Mapping) or rendered == "malformed":
        return DimensionVerdict(
            dimension,
            UNDECIDABLE,
            rendered,
            reader,
            detail=(
                "entry declares this dimension in a shape this service does not "
                "understand; treat applicability on it as unknown"
            ),
        )

    if constraint.get("any") is True:
        basis = constraint.get("basis")
        detail = "matched on an unproven independence claim, not on an observation"
        if reader is None:
            detail += "; the reader also supplied no value for this dimension"
        return DimensionVerdict(
            dimension,
            ASSUMED_ANY,
            rendered,
            reader,
            basis=str(basis) if basis else None,
            detail=detail,
        )

    if reader is None:
        return DimensionVerdict(
            dimension,
            UNCHECKED,
            rendered,
            None,
            detail="reader coordinate supplied no value, so this dimension was not checked",
        )

    values = constraint.get("values")
    if isinstance(values, (list, tuple)):
        wanted = {_norm(v).lower() for v in values}
        if reader.lower() in wanted:
            return DimensionVerdict(dimension, COVERED, rendered, reader)
        return DimensionVerdict(
            dimension,
            MISMATCH,
            rendered,
            reader,
            detail=f"entry was established on {rendered}, which does not include {reader!r}",
        )

    rng = constraint.get("range")
    if isinstance(rng, Mapping):
        low, high = rng.get("min"), rng.get("max")
        if low is None and high is None:
            # Unbounded on both sides is not a bound: nobody established
            # anything about this dimension. Reporting `covered` here would
            # tell the reader their value was *observed* to fit, which is the
            # inverse of the doctrine that an absent fact means unknown. It is
            # the honest encoding of an unresolved dimension, so it is
            # reported as unknown rather than as a match.
            return DimensionVerdict(
                dimension,
                UNDECIDABLE,
                rendered,
                reader,
                detail=(
                    "the entry bounds this dimension on neither side, so it is "
                    "untested territory: nothing about this dimension was "
                    "established and applicability on it is unknown"
                ),
            )
        for bound, label in ((low, "min"), (high, "max")):
            if bound is None:
                continue
            cmp = _compare_versions(reader, str(bound))
            if cmp is None:
                return DimensionVerdict(
                    dimension,
                    UNDECIDABLE,
                    rendered,
                    reader,
                    detail=(
                        f"cannot order {reader!r} against {label}={bound!r}; "
                        "applicability on this dimension is unknown"
                    ),
                )
            if label == "min" and cmp < 0:
                return DimensionVerdict(
                    dimension,
                    MISMATCH,
                    rendered,
                    reader,
                    detail=f"{reader} is below the established minimum {bound}",
                )
            if label == "max" and cmp > 0:
                return DimensionVerdict(
                    dimension,
                    MISMATCH,
                    rendered,
                    reader,
                    detail=f"{reader} is above the established maximum {bound}",
                )
        return DimensionVerdict(dimension, COVERED, rendered, reader)

    return DimensionVerdict(dimension, UNDECIDABLE, rendered, reader, detail="unrecognized constraint")


@dataclass
class Applicability:
    verdicts: list[DimensionVerdict] = field(default_factory=list)

    @property
    def by_verdict(self) -> dict[str, list[DimensionVerdict]]:
        out: dict[str, list[DimensionVerdict]] = {}
        for verdict in self.verdicts:
            out.setdefault(verdict.verdict, []).append(verdict)
        return out

    @property
    def applies(self) -> bool:
        return not any(v.verdict == MISMATCH for v in self.verdicts)

    @property
    def covered_count(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict == COVERED)

    @property
    def assumption_count(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict == ASSUMED_ANY)

    def to_dict(self) -> dict[str, Any]:
        grouped = self.by_verdict
        return {
            "applies": self.applies,
            "dimensions": [v.to_dict() for v in self.verdicts],
            "covered": [v.dimension for v in grouped.get(COVERED, [])],
            "matched_on_independence_claim": [
                {"dimension": v.dimension, "basis": v.basis} for v in grouped.get(ASSUMED_ANY, [])
            ],
            "unchecked": [v.dimension for v in grouped.get(UNCHECKED, [])],
            "undecidable": [
                {"dimension": v.dimension, "detail": v.detail} for v in grouped.get(UNDECIDABLE, [])
            ],
            "mismatched": [
                {
                    "dimension": v.dimension,
                    "reader_value": v.reader_value,
                    "entry_constraint": v.constraint,
                    "detail": v.detail,
                }
                for v in grouped.get(MISMATCH, [])
            ],
        }


def normalize_coordinate(coordinate: Mapping[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """Keep only known dimensions with non-empty values; report the rest."""

    if not coordinate:
        return {}, []
    kept: dict[str, str] = {}
    ignored: list[str] = []
    for key, value in coordinate.items():
        name = str(key).strip().lower()
        if name not in SCOPE_DIMENSIONS:
            ignored.append(str(key))
            continue
        if value in (None, ""):
            continue
        kept[name] = _norm(value)
    return kept, ignored


def evaluate_scope(scope: Any, coordinate: Mapping[str, str]) -> Applicability:
    verdicts: list[DimensionVerdict] = []
    scope_map = scope if isinstance(scope, Mapping) else {}
    for dimension in SCOPE_DIMENSIONS:
        if dimension not in scope_map:
            # schemas/knowledge-v2.schema.json forbids this, but the service
            # reads files it did not write; an incomplete coordinate is
            # reported as unknown, never as a match.
            verdicts.append(
                DimensionVerdict(
                    dimension,
                    UNDECIDABLE,
                    "absent",
                    coordinate.get(dimension),
                    detail=(
                        "entry does not declare this dimension, which schema v2 "
                        "requires; applicability on it is unknown"
                    ),
                )
            )
            continue
        verdicts.append(
            evaluate_dimension(dimension, scope_map[dimension], coordinate.get(dimension))
        )
    return Applicability(verdicts)


# --------------------------------------------------------------------------
# text / fingerprint matching
# --------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _fingerprints(rule: Mapping[str, Any]) -> list[str]:
    raw = rule.get("fingerprints")
    if not isinstance(raw, (list, tuple)):
        return []
    return [_WS_RE.sub(" ", str(item).strip().lower()) for item in raw if str(item).strip()]


@dataclass
class TextMatch:
    score: float = 0.0
    matched_fingerprints: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)


BODY_KEYS = ("rule", "measurement")


def entry_body(entry: Mapping[str, Any]) -> str:
    """``"rule"`` or ``"measurement"``; ``"rule"`` for anything malformed.

    Exactly one body is guaranteed by the schema. Malformed entries are still
    reported (rather than dropped) so a broken file is visible, and treating
    them as rules keeps every v1 field populated with whatever is there.
    """
    if isinstance(entry.get("measurement"), Mapping) and not isinstance(entry.get("rule"), Mapping):
        return "measurement"
    return "rule"


def searchable_view(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """A rule-shaped view of whichever body the entry carries.

    Text search is defined over the rule fields (weights in ``_FIELD_WEIGHTS``)
    and over ``fingerprints`` as exact-ish tokens. A measurement has no
    symptom, but it has a subject with aliases and named quantities, and a
    reader who asks for ``"910B4 fp16 peak"`` should find it. The subject id,
    its aliases and the quantity names therefore play the fingerprint role;
    the summary and the method description play the prose role.
    """
    if entry_body(entry) == "rule":
        return entry.get("rule") if isinstance(entry.get("rule"), Mapping) else {}
    m = entry["measurement"]
    subject = m.get("subject") if isinstance(m.get("subject"), Mapping) else {}
    method = m.get("method") if isinstance(m.get("method"), Mapping) else {}
    quantities = m.get("quantities") if isinstance(m.get("quantities"), list) else []
    tokens: list[str] = []
    if subject.get("id"):
        tokens.append(str(subject["id"]))
    tokens.extend(str(a) for a in (subject.get("aliases") or []) if isinstance(a, str))
    for q in quantities:
        if isinstance(q, Mapping):
            if q.get("name"):
                tokens.append(str(q["name"]))
            if q.get("name") and q.get("basis"):
                tokens.append(f"{q['name']} {q['basis']}")
    return {
        "summary": m.get("summary"),
        "resolution": method.get("description"),
        "fingerprints": tokens,
    }


def score_text(rule: Mapping[str, Any], slug: str, query: str | None, fingerprint: str | None) -> TextMatch:
    match = TextMatch()
    fingerprints = _fingerprints(rule)

    if fingerprint:
        needle = _WS_RE.sub(" ", fingerprint.strip().lower())
        for candidate in fingerprints:
            if needle == candidate:
                match.score += _FINGERPRINT_WEIGHT * 3
                match.matched_fingerprints.append(candidate)
            elif needle in candidate or candidate in needle:
                match.score += _FINGERPRINT_WEIGHT
                match.matched_fingerprints.append(candidate)

    if query:
        q = query.lower()
        terms = set(_tokens(q))
        if terms:
            for candidate in fingerprints:
                if candidate and candidate in q:
                    match.score += _FINGERPRINT_WEIGHT
                    if candidate not in match.matched_fingerprints:
                        match.matched_fingerprints.append(candidate)
                    continue
                hits = [t for t in terms if t in candidate]
                if hits:
                    match.score += _FINGERPRINT_WEIGHT * len(hits) / max(len(terms), 1)
            for field_name, weight in _FIELD_WEIGHTS.items():
                text = str(rule.get(field_name) or "").lower()
                if not text:
                    continue
                hits = sorted({t for t in terms if t in text})
                if hits:
                    match.score += weight * len(hits) / len(terms)
                    match.matched_terms.extend(hits)
            slug_text = slug.lower()
            slug_hits = sorted({t for t in terms if t in slug_text})
            if slug_hits:
                match.score += _SLUG_WEIGHT * len(slug_hits) / len(terms)
                match.matched_terms.extend(slug_hits)

    match.matched_terms = sorted(set(match.matched_terms))
    return match


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


def _parse_date(value: Any) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _staleness(entry: Mapping[str, Any], stale_after_days: int, today: _dt.date) -> dict[str, Any]:
    verification = entry.get("verification") if isinstance(entry.get("verification"), Mapping) else {}
    last = _parse_date(verification.get("last_verified_at"))
    info: dict[str, Any] = {"last_verified_at": str(verification.get("last_verified_at") or "") or None}
    if last is None:
        info["age_days"] = None
        info["policy_horizon_days"] = stale_after_days
        info["policy_horizon_exceeded"] = None
        return info
    age = (today - last).days
    info["age_days"] = age
    info["policy_horizon_days"] = stale_after_days
    info["policy_horizon_exceeded"] = age > stale_after_days
    return info


@dataclass
class Result:
    loaded: LoadedEntry
    applicability: Applicability
    text: TextMatch
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    shadowed: list[dict[str, Any]] = field(default_factory=list)
    staleness: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    @property
    def entry(self) -> Mapping[str, Any]:
        return self.loaded.entry

    def to_dict(self) -> dict[str, Any]:
        entry = self.entry
        provenance = entry.get("provenance") if isinstance(entry.get("provenance"), Mapping) else {}
        lifecycle = entry.get("lifecycle") if isinstance(entry.get("lifecycle"), Mapping) else {}
        verification = (
            entry.get("verification") if isinstance(entry.get("verification"), Mapping) else {}
        )
        body = entry_body(entry)
        rule = entry.get("rule") if isinstance(entry.get("rule"), Mapping) else {}
        measurement = entry.get("measurement") if isinstance(entry.get("measurement"), Mapping) else {}
        # ``summary`` is the one prose field both bodies share. The three
        # rule-only fields stay in the payload (a v1 caller indexes them) and
        # are null for a measurement, which is the v1-visible signal that this
        # result is not a failure rule; ``body`` is the v2 signal.
        summary = rule.get("summary") if body == "rule" else measurement.get("summary")

        out: dict[str, Any] = {
            "uuid": entry.get("uuid"),
            "slug": entry.get("slug"),
            "kind": self.loaded.kind,
            "layer": self.loaded.layer,
            "status": entry.get("status"),
            "confidence": entry.get("confidence"),
            "content_hash": entry.get("content_hash"),
            "body": body,
            "summary": summary,
            "symptom": rule.get("symptom"),
            "root_cause": rule.get("root_cause"),
            "resolution": rule.get("resolution"),
            "provenance": {
                "origin_repo": provenance.get("origin_repo"),
                "contributor": provenance.get("contributor"),
                "submitted_at": provenance.get("submitted_at"),
                "redaction_profile": provenance.get("redaction_profile"),
            },
            "evidence": [
                {"type": item.get("type"), "ref": item.get("ref")}
                for item in (verification.get("evidence") or [])
                if isinstance(item, Mapping)
            ],
            "verified_by": list(verification.get("verified_by") or []),
            "lifecycle": {
                "first_seen": lifecycle.get("first_seen"),
                "updated_at": lifecycle.get("updated_at"),
                "superseded_by": lifecycle.get("superseded_by"),
                "resolved_by": lifecycle.get("resolved_by"),
            },
            "staleness": self.staleness,
            "applicability": self.applicability.to_dict(),
            "match": {
                "score": round(self.score, 4),
                "text_score": round(self.text.score, 4),
                "matched_fingerprints": self.text.matched_fingerprints,
                "matched_terms": self.text.matched_terms,
            },
            "source": self.loaded.origin(),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }
        if body == "measurement":
            out["measurement"] = {
                "subject": measurement.get("subject"),
                "method": measurement.get("method"),
                "quantities": measurement.get("quantities"),
            }
        if self.shadowed:
            out["also_present_in"] = self.shadowed
        if entry.get("conflicts"):
            out["conflicts"] = entry.get("conflicts")
        return out

    def sort_key(self) -> tuple:
        return (
            0 if self.applicability.applies else 1,
            -round(self.score, 6),
            STATUS_RANK.get(str(self.entry.get("status")), 9),
            CONFIDENCE_RANK.get(str(self.entry.get("confidence")), 9),
            LAYER_PRECEDENCE.get(self.loaded.layer, 9),
            str(self.entry.get("uuid", "")),
        )


@dataclass
class QueryResponse:
    results: list[Result]
    config: ServiceConfig
    load: LoadReport
    request: dict[str, Any]
    ignored_coordinate_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        coordinate: dict[str, Any] = self.request.get("reader_coordinate") or {}
        missing = [d for d in READER_DIMENSIONS if d not in coordinate]
        consulted = self.config.consulted(self.request.get("layers"))
        payload = {
            "version": package_version(),
            "request": self.request,
            "reader_coordinate": {
                "supplied": coordinate,
                "unsupplied_expected_dimensions": missing,
                "ignored_keys": self.ignored_coordinate_keys,
            },
            "layers_available": consulted["layers_available"],
            "layers_absent": consulted["layers_absent"],
            "degraded": consulted["degraded"],
            "absent_fact_semantics": "unknown",
            "no_result_meaning": NO_RESULT_MEANING,
            "load": self.load.describe(),
            "warnings": list(self.config.warnings),
            "notes": list(self.notes),
            "count": len(self.results),
            "results": [r.to_dict() for r in self.results],
        }
        payload.update(shared_source())
        return payload


# --------------------------------------------------------------------------
# the query entry point
# --------------------------------------------------------------------------


def resolve_statuses(
    config: ServiceConfig,
    include_unverified: bool,
    statuses: Sequence[str] | None,
) -> tuple[str, ...]:
    if statuses:
        return tuple(dict.fromkeys(str(s) for s in statuses))
    wanted = list(config.default_statuses or DEFAULT_STATUSES)
    if include_unverified:
        for status in OPT_IN_STATUSES:
            if status not in wanted:
                wanted.append(status)
    return tuple(wanted)


def query(
    config: ServiceConfig,
    *,
    text: str | None = None,
    fingerprint: str | None = None,
    reader_coordinate: Mapping[str, Any] | None = None,
    layers: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    include_unverified: bool = False,
    include_non_matching: bool = False,
    kind: str | None = None,
    bodies: Sequence[str] | None = None,
    limit: int = 20,
    today: _dt.date | None = None,
    load: LoadReport | None = None,
) -> QueryResponse:
    """Search the mounted layers.

    Defaults follow docs/lifecycle.md: `verified`, `stale` (with a warning)
    and `resolved` (with its fix reference) come back; `unverified` requires
    ``include_unverified=True``; `deprecated` and superseded entries are out.

    ``bodies`` restricts the payload variants returned. The default is every
    body the service knows (``rule`` and ``measurement``).
    """

    today = today or _dt.date.today()
    wanted_bodies = [b for b in (bodies or BODY_KEYS) if b in BODY_KEYS]
    if bodies is not None and not wanted_bodies:
        wanted_bodies = list(BODY_KEYS)
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYER_PRECEDENCE]
    default_layers = layers is None
    if default_layers:
        # README.md: the default result set is shared + project. The candidate
        # layer holds a developer's own unreviewed notes, so it rides on the
        # same opt-in as unverified status -- asking for unverified knowledge
        # and not getting your own captures back would be surprising, and
        # candidate entries cannot pass the default status filter anyway.
        wanted_layers = ["shared", "project", "candidate"] if include_unverified else ["shared", "project"]

    coordinate, ignored = normalize_coordinate(reader_coordinate)
    wanted_statuses = resolve_statuses(config, include_unverified, statuses)

    report = load if load is not None else load_entries(config, wanted_layers)
    notes: list[str] = []

    # Duplicate uuids across layers: keep the most trusted copy, but say that
    # the others exist, and say so loudly when the revisions differ.
    best: dict[str, LoadedEntry] = {}
    shadowed: dict[str, list[dict[str, Any]]] = {}
    for loaded in report.entries:
        if loaded.layer not in wanted_layers:
            continue
        uid = loaded.uuid
        current = best.get(uid)
        if current is None:
            best[uid] = loaded
            continue
        keep, drop = (
            (current, loaded)
            if LAYER_PRECEDENCE[current.layer] <= LAYER_PRECEDENCE[loaded.layer]
            else (loaded, current)
        )
        best[uid] = keep
        record = {
            "layer": drop.layer,
            "file": drop.source,
            "status": drop.status,
            "content_hash": drop.entry.get("content_hash"),
        }
        if drop.entry.get("content_hash") != keep.entry.get("content_hash"):
            record["revision_divergence"] = (
                "this layer holds a different revision of the same uuid; "
                "the higher-trust layer was returned"
            )
        shadowed.setdefault(uid, []).append(record)

    results: list[Result] = []
    filtered_out = {"status": 0, "coordinate": 0, "kind": 0, "body": 0, "superseded": 0, "text": 0}

    for uid, loaded in best.items():
        entry = loaded.entry
        status = str(entry.get("status", "unverified"))

        if kind and loaded.kind != kind:
            filtered_out["kind"] += 1
            continue
        if entry_body(entry) not in wanted_bodies:
            filtered_out["body"] += 1
            continue
        if status not in wanted_statuses:
            filtered_out["status"] += 1
            continue

        lifecycle = entry.get("lifecycle") if isinstance(entry.get("lifecycle"), Mapping) else {}
        if lifecycle.get("superseded_by") and statuses is None:
            # docs/lifecycle.md: the superseded entry stays in the corpus but
            # drops out of default results.
            filtered_out["superseded"] += 1
            continue

        applicability = evaluate_scope(entry.get("scope"), coordinate)
        if not applicability.applies and not include_non_matching:
            filtered_out["coordinate"] += 1
            continue

        text_match = score_text(searchable_view(entry), str(entry.get("slug") or ""), text, fingerprint)
        if (text or fingerprint) and text_match.score <= 0:
            filtered_out["text"] += 1
            continue

        warnings: list[str] = []
        notes_for_entry: list[str] = []

        if status == "stale":
            warnings.append(STALE_WARNING)
        if status == "resolved":
            resolved_by = lifecycle.get("resolved_by") or {}
            ref = resolved_by.get("ref") if isinstance(resolved_by, Mapping) else None
            kind_of_fix = resolved_by.get("type") if isinstance(resolved_by, Mapping) else None
            warnings.append(
                "status=resolved: the underlying problem was fixed"
                + (f" by {kind_of_fix} {ref}" if ref else "")
                + ". It still applies to anyone on an affected version."
            )
        if status == "unverified":
            warnings.append(
                "status=unverified: a single unconfirmed observation with no "
                "non-submitter confirmation. Treat as a lead, not as a fact."
            )
        if loaded.layer == "candidate":
            warnings.append(
                "layer=candidate: unreviewed local capture, not part of any "
                "reviewed corpus."
            )
        elif loaded.layer == "project":
            notes_for_entry.append(
                "layer=project: tied to this repo's checkout and may not be publishable."
            )

        for verdict in applicability.by_verdict.get(ASSUMED_ANY, []):
            warnings.append(
                f"dimension {verdict.dimension} matched on an unproven independence "
                f"claim (scope.{verdict.dimension}.any). Basis: "
                f"{verdict.basis or 'none recorded'}"
            )
        unchecked = [v.dimension for v in applicability.by_verdict.get(UNCHECKED, [])]
        if unchecked:
            notes_for_entry.append(
                "not checked against your build on: " + ", ".join(unchecked)
            )
        for verdict in applicability.by_verdict.get(UNDECIDABLE, []):
            warnings.append(f"dimension {verdict.dimension}: {verdict.detail}")
        if not applicability.applies:
            warnings.append(
                "DOES NOT APPLY to the supplied reader coordinate: "
                + "; ".join(
                    f"{v.dimension} {v.reader_value!r} vs {v.constraint}"
                    for v in applicability.by_verdict.get(MISMATCH, [])
                )
            )

        staleness = _staleness(entry, config.stale_after_days, today)
        if status == "verified" and staleness.get("policy_horizon_exceeded"):
            warnings.append(
                "last_verified_at is older than the configured staleness horizon "
                f"({staleness['age_days']}d > {staleness['policy_horizon_days']}d) "
                "but the entry is still marked verified. The staleness sweep has "
                "not run over this layer."
            )

        result = Result(
            loaded=loaded,
            applicability=applicability,
            text=text_match,
            warnings=warnings,
            notes=notes_for_entry,
            shadowed=shadowed.get(uid, []),
            staleness=staleness,
        )
        result.score = _rank_score(result, bool(text or fingerprint))
        results.append(result)

    results.sort(key=lambda r: r.sort_key())
    if limit and limit > 0:
        results = results[:limit]

    notes.append(
        "filtered out: "
        + ", ".join(f"{k}={v}" for k, v in filtered_out.items() if v)
        if any(filtered_out.values())
        else "nothing was filtered out"
    )
    if filtered_out["coordinate"] and not include_non_matching:
        notes.append(
            f"{filtered_out['coordinate']} entry/entries were dropped because the "
            "reader coordinate falls outside their established scope. Pass "
            "include_non_matching=true to see them, labelled as not applying."
        )
    if not config.available_layers():
        notes.append(
            "No layer is mounted. Every answer from this service is therefore "
            "'unknown', never 'supported'."
        )

    return QueryResponse(
        results=results,
        config=config,
        load=report,
        request={
            "text": text,
            "fingerprint": fingerprint,
            "reader_coordinate": coordinate,
            "layers": wanted_layers,
            "statuses": list(wanted_statuses),
            "include_unverified": include_unverified,
            "include_non_matching": include_non_matching,
            "kind": kind,
            "bodies": wanted_bodies,
            "limit": limit,
        },
        ignored_coordinate_keys=ignored,
        notes=notes,
    )


def _rank_score(result: Result, has_text_query: bool) -> float:
    """Rank by how well the entry is *known* to fit, not by prose overlap.

    A bounded dimension that the reader actually satisfies is worth more than
    an `any` claim, because the first was observed and the second was asserted.
    """

    app = result.applicability
    score = 0.0
    if has_text_query:
        score += result.text.score
    score += 1.2 * app.covered_count
    score += 0.15 * app.assumption_count
    score -= 0.3 * len(app.by_verdict.get(UNDECIDABLE, []))
    if not app.applies:
        score -= 10.0
    score += {"verified": 1.0, "stale": 0.4, "resolved": 0.4}.get(
        str(result.entry.get("status")), 0.0
    )
    score += {"high": 0.5, "medium": 0.2}.get(str(result.entry.get("confidence")), 0.0)
    score += {"shared": 0.3, "project": 0.2}.get(result.loaded.layer, 0.0)
    return score


# --------------------------------------------------------------------------
# explain
# --------------------------------------------------------------------------


def explain(
    config: ServiceConfig,
    uuid: str,
    *,
    reader_coordinate: Mapping[str, Any] | None = None,
    layers: Sequence[str] | None = None,
    today: _dt.date | None = None,
    load: LoadReport | None = None,
) -> dict[str, Any]:
    """Expand one entry by uuid into its full record.

    A uuid that is not present returns ``found: false`` with an explicit
    "unknown" reading, and lists which layers were actually consulted -- an
    absent entry in a degraded service says nothing about the world.
    """

    today = today or _dt.date.today()
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYER_PRECEDENCE]
    report = load if load is not None else load_entries(config, wanted_layers)
    coordinate, ignored = normalize_coordinate(reader_coordinate)

    matches = [e for e in report.entries if e.uuid == uuid and e.layer in wanted_layers]
    consulted = config.consulted(wanted_layers)
    base: dict[str, Any] = {
        "version": package_version(),
        "uuid": uuid,
        "layers_consulted": consulted["layers_available"],
        "layers_absent": consulted["layers_absent"],
        "degraded": consulted["degraded"],
        "absent_fact_semantics": "unknown",
        "load": report.describe(),
    }
    base.update(shared_source())

    if not matches:
        base.update(
            found=False,
            meaning=(
                "No entry with this uuid in the consulted layers. That is 'unknown': "
                "the entry may exist in a layer that is not mounted."
            ),
        )
        return base

    matches.sort(key=lambda e: LAYER_PRECEDENCE.get(e.layer, 9))
    primary = matches[0]
    entry = primary.entry
    applicability = evaluate_scope(entry.get("scope"), coordinate)

    base.update(
        found=True,
        layer=primary.layer,
        kind=primary.kind,
        document_layer=primary.document_layer,
        source=primary.origin(),
        entry=dict(entry),
        applicability=applicability.to_dict(),
        reader_coordinate={"supplied": coordinate, "ignored_keys": ignored},
        staleness=_staleness(entry, config.stale_after_days, today),
        warnings=(
            [STALE_WARNING] if str(entry.get("status")) == "stale" else []
        ),
        other_copies=[
            {
                "layer": other.layer,
                "file": other.source,
                "status": other.status,
                "content_hash": other.entry.get("content_hash"),
            }
            for other in matches[1:]
        ],
    )
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vaws-knowledge query",
        description="Search a local corpus checkout and print JSON results.",
    )
    parser.add_argument("--corpus", help="corpus root or checkout containing corpus/verified/")
    parser.add_argument("--config", help="path to a JSON/YAML service config")
    parser.add_argument("--text", help="free-text query")
    parser.add_argument("--fingerprint", help="fingerprint string to match")
    parser.add_argument("--kind", help="restrict to one document kind")
    parser.add_argument("--layers", help="comma-separated layers (shared,project,candidate)")
    parser.add_argument("--bodies", help="comma-separated body variants (rule,measurement)")
    parser.add_argument("--include-unverified", action="store_true")
    parser.add_argument("--include-non-matching", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.corpus:
        env[ENV_CORPUS] = args.corpus
    config = load_config(path=args.config, env=env)
    layers = [item.strip() for item in args.layers.split(",")] if args.layers else None
    bodies = [item.strip() for item in args.bodies.split(",")] if args.bodies else None
    response = query(
        config,
        text=args.text,
        fingerprint=args.fingerprint,
        layers=layers,
        include_unverified=args.include_unverified,
        include_non_matching=args.include_non_matching,
        kind=args.kind,
        bodies=bodies,
        limit=args.limit,
    )
    print(json.dumps(response.to_dict(), indent=2, ensure_ascii=False))
    return 0
