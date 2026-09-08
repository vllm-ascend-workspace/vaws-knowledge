"""Version ordering for scope bounds, per ``docs/version-ordering.md``.

That document is normative and this module implements it. It exists because
three independent implementations in this repository each invented their own
ordering, and two of them disagreed about whether ``8.0.RC10`` is newer than
``8.0.RC2`` — which is enough to make the same entry apply for one reader and
not for another.

This module replaces an earlier version that applied one universal rule:
extract a dotted numeric prefix, and call it undecidable whenever the prefixes
matched but the suffixes differed. That rule disagreed with the specification on
four of its six worked examples, all in the conservative direction:

    8.0.RC2   vs 8.0.RC3     cann   spec A<B, old rule undecidable
    8.0.RC2   vs 8.0.RC10    cann   spec A<B, old rule undecidable
    2.5.1     vs 2.5.1.post1 torch  spec A<B, old rule undecidable
    0.11.0rc1 vs 0.11.0      vllm   spec A<B, old rule undecidable

Conservative is not harmless here. Every one of those is a comparison the
corpus actually makes, and the conflict gate turns an undecidable comparison
into an ``undeclared_dimension``. Over-reporting them inflates confounders and
blocks promotions that should proceed, which makes the conflict mechanism noisy
enough to be ignored — a different failure, not a safe one.

Three outcomes, never two: less, greater-or-equal, and **undecidable**.
``undecidable`` is never collapsed into either of the others and never resolved
by picking the more likely reading. A reader told "these two version strings are
not comparable" goes and checks; a reader told "applies" does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Ordering scheme per dimension. Different dimensions follow genuinely
# different conventions, so one universal rule would be wrong for most of them.
PEP440_DIMENSIONS = frozenset({"torch", "torch_npu", "vllm", "vllm_ascend"})
NATURAL_DIMENSIONS = frozenset({"cann", "driver"})
EXACT_ONLY_DIMENSIONS = frozenset(
    {"soc", "python_abi", "model", "topology", "execution_mode", "component"}
)

SCHEMES: dict[str, str] = {
    **{d: "pep440" for d in PEP440_DIMENSIONS},
    **{d: "natural" for d in NATURAL_DIMENSIONS},
    **{d: "exact" for d in EXACT_ONLY_DIMENSIONS},
}


class RangeNotAllowed(ValueError):
    """A ``range`` constraint on a dimension that is exact-match-only.

    Deliberately distinct from an undecidable comparison: the entry is
    malformed rather than ambiguous, so reporting it as ``undecidable`` would
    hide a contract violation behind a legitimate-looking outcome.
    """


def scheme_for(dimension: str) -> str:
    """Ordering scheme for a dimension. Unknown dimensions are exact-only.

    Failing closed on an unknown name matters because the schema can grow a
    dimension before this table does, and inventing an ordering for a dimension
    nobody has thought about is exactly the guessing this module refuses to do.
    """
    return SCHEMES.get(dimension, "exact")


def range_allowed(dimension: str) -> bool:
    return scheme_for(dimension) != "exact"


# --- unbounded sentinels ----------------------------------------------------


@dataclass(frozen=True)
class Unbounded:
    """``null`` on one side of a range: always satisfied on that side."""

    sign: int


NEG_INF = Unbounded(-1)
POS_INF = Unbounded(1)


def bound_or_inf(value: object, side: str) -> object:
    """A range bound, with ``None`` meaning unbounded on ``side``."""
    if value is None:
        return NEG_INF if side == "min" else POS_INF
    return value


# --- natural segment ordering (cann, driver) --------------------------------

_BUILD_METADATA = re.compile(r"\+.*$")
_SEGMENT_SPLIT = re.compile(r"[._-]+")
_RUNS = re.compile(r"\d+|\D+")


def _natural_segments(text: str) -> Optional[list[list[object]]]:
    """Split into segments, then each segment into digit / non-digit runs.

    Run decomposition is why ``8.0.RC10`` sorts above ``8.0.RC2``: comparing
    the whole segment as text would order ``RC10`` first, which is wrong and
    was one of the disagreements between the earlier implementations.
    """
    cleaned = _BUILD_METADATA.sub("", str(text).strip())
    if not cleaned:
        return None
    segments: list[list[object]] = []
    for segment in _SEGMENT_SPLIT.split(cleaned):
        if not segment:
            continue
        runs: list[object] = []
        for run in _RUNS.findall(segment):
            runs.append(int(run) if run.isdigit() else run.lower())
        if runs:
            segments.append(runs)
    return segments or None


def _compare_runs(a: list[object], b: list[object]) -> Optional[int]:
    for run_a, run_b in zip(a, b):
        a_num, b_num = isinstance(run_a, int), isinstance(run_b, int)
        if a_num != b_num:
            # A digit run against a letter run. Semantic versioning would rank
            # these, but the vendor schemes here do not define a relation, and
            # guessing one is how a fact established on one CANN release gets
            # applied to another.
            return None
        if run_a != run_b:
            return -1 if run_a < run_b else 1  # type: ignore[operator]
    if len(a) == len(b):
        return 0
    longer, sign = (b, -1) if len(a) < len(b) else (a, 1)
    extra = longer[min(len(a), len(b)) :]
    if all(isinstance(run, int) for run in extra):
        return sign
    return None


def _compare_natural(a: str, b: str) -> Optional[int]:
    sa, sb = _natural_segments(a), _natural_segments(b)
    if sa is None or sb is None:
        return None
    for seg_a, seg_b in zip(sa, sb):
        result = _compare_runs(seg_a, seg_b)
        if result is None:
            return None
        if result != 0:
            return result
    if len(sa) == len(sb):
        return 0
    longer, sign = (sb, -1) if len(sa) < len(sb) else (sa, 1)
    extra = longer[min(len(sa), len(sb)) :]
    # `8.0.RC2` against `8.0` is undecidable rather than greater: a release
    # candidate is not obviously newer or older than the bare version, and the
    # vendor scheme does not tell us.
    if all(all(isinstance(run, int) for run in seg) for seg in extra):
        return sign
    return None


# --- PEP 440 ordering (torch, torch_npu, vllm, vllm_ascend) -----------------


def _pep440_available() -> bool:
    try:
        import packaging.version  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _compare_pep440(a: str, b: str) -> Optional[int]:
    """PEP 440 semantics, or undecidable when they cannot be evaluated.

    When ``packaging`` is unavailable this returns ``None`` rather than falling
    back to the natural rule. A silent substitution of one ordering for another
    is the divergence the specification exists to prevent, and it would be
    invisible: the results stay plausible.
    """
    try:
        from packaging.version import InvalidVersion, Version  # noqa: PLC0415
    except ImportError:
        return None
    try:
        va, vb = Version(str(a)), Version(str(b))
    except InvalidVersion:
        return None
    if va == vb:
        return 0
    return -1 if va < vb else 1


# --- public comparison ------------------------------------------------------


def compare(a: object, b: object, *, dimension: str) -> Optional[int]:
    """``-1`` / ``0`` / ``1``, or ``None`` when the order is undecidable.

    ``dimension`` is required: the ordering scheme depends on it, and the
    earlier dimension-agnostic signature is precisely how one rule ended up
    applied to conventions it did not fit.
    """
    if isinstance(a, Unbounded) and isinstance(b, Unbounded):
        if a.sign == b.sign:
            return 0
        return -1 if a.sign < b.sign else 1
    if isinstance(a, Unbounded):
        return a.sign
    if isinstance(b, Unbounded):
        return -b.sign

    text_a, text_b = str(a).strip(), str(b).strip()
    scheme = scheme_for(dimension)
    if scheme == "exact":
        raise RangeNotAllowed(
            f"dimension {dimension!r} is exact-match-only and must not carry a range; "
            f"use `values` or `any` (comparing {text_a!r} with {text_b!r})"
        )
    if scheme == "pep440":
        return _compare_pep440(text_a, text_b)
    return _compare_natural(text_a, text_b)


def within(value: object, minimum: object, maximum: object, *, dimension: str) -> Optional[bool]:
    """Is ``value`` inside an inclusive range? ``None`` when undecidable.

    A single undecidable comparison makes the whole containment question
    undecidable, rather than being treated as a miss. Reporting a miss would
    read as "this entry does not apply to you", which is a claim nobody
    established.
    """
    low = compare(bound_or_inf(minimum, "min"), value, dimension=dimension)
    if low is None:
        return None
    if low > 0:
        return False
    high = compare(value, bound_or_inf(maximum, "max"), dimension=dimension)
    if high is None:
        return None
    return high <= 0
