"""Run every review gate in order and collect deterministic results.

Gate order (see docs/review-pipeline.md):

1. ``load``        — every YAML file parses and has an ``entries`` list
2. ``schema``      — ``tools/validate.py <paths>``            (external CLI)
3. ``redaction``   — ``tools/redact.py --check <paths>``      (external CLI)
4. ``canonical``   — ``tools/canonical.py --check <paths>``   (external CLI)
5. ``integrity``   — ``bot/integrity.py``
6. ``duplicates``  — ``bot/dedup.py``
7. ``conflicts``   — ``bot/conflicts.py``
8. ``staleness``   — ``bot/staleness.py`` (advisory in PR mode, finding in audit mode)

Fail closed: an external gate whose tool is missing, crashes or times out is
reported as ``unavailable`` / ``error`` and counts as a failure. Internal
gates that cannot run because loading failed are ``skipped`` and the run is
already failing. There is no path through this module where a gate that did
not run reads as a pass.

External tools are invoked as CLI contracts and never imported; this package
does not depend on the layout of ``tools/``.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import conflicts as _conflicts
from bot import dedup as _dedup
from bot import integrity as _integrity
from bot import staleness as _staleness
from bot.corpus import DependencyError, LoadResult, load_paths, relpath, repo_root
from bot.policy import PolicyError, load_policy

PASS, WARN, FAIL, UNAVAILABLE, ERROR, SKIPPED = (
    "pass",
    "warn",
    "fail",
    "unavailable",
    "error",
    "skipped",
)

OK_STATUSES = frozenset({PASS, WARN})

EXTERNAL_TIMEOUT_SECONDS = 600
MAX_OUTPUT_LINES = 40

EXTERNAL_GATES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # id, title, script, fixed args
    ("schema", "Schema conformance and content_hash", "tools/validate.py", ()),
    ("redaction", "Redaction re-scan", "tools/redact.py", ("--check",)),
    # There is deliberately no separate canonical-hash gate. An earlier draft of
    # this file had one, invoking `tools/canonical.py --check` — a flag that tool
    # has never had, because the draft was written against an assumed interface
    # while tools/ was being built in parallel. The gate could not have passed on
    # any pull request.
    #
    # It was also redundant: tools/validate.py already recomputes content_hash
    # and rejects a mismatch. Adding the flag to satisfy this file would have
    # grown the tool's surface to serve a duplicate check, so the gate is gone
    # and the schema gate's title now says what it actually covers.
)


@dataclass
class GateResult:
    id: str
    title: str
    status: str
    blocking: bool
    summary: str
    details: list[str] = field(default_factory=list)
    data: Optional[dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "blocking": self.blocking,
            "summary": self.summary,
            "details": list(self.details),
        }
        if self.data is not None:
            out["data"] = self.data
        return out


# ---------------------------------------------------------------------------
# external CLI gates
# ---------------------------------------------------------------------------


def _scrub(text: str, root: Path) -> str:
    """Keep tool output relative so the review comment never leaks runner paths."""
    return text.replace(str(root.resolve()) + "/", "").replace(str(root.resolve()), ".")


def run_external_gate(
    gate_id: str,
    title: str,
    script: str,
    fixed_args: Sequence[str],
    paths: Sequence[str],
    root: Path,
    *,
    python: str = sys.executable,
    timeout: int = EXTERNAL_TIMEOUT_SECONDS,
) -> GateResult:
    script_path = root / script
    if not script_path.is_file():
        return GateResult(
            gate_id,
            title,
            UNAVAILABLE,
            True,
            f"gate unavailable → fail closed: `{script}` is not present in this checkout",
            [f"Expected `{script}` to exist. The bot does not reimplement it."],
        )
    argv = [python, str(script_path), *fixed_args, *paths]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            gate_id,
            title,
            ERROR,
            True,
            f"gate error → fail closed: `{script}` exceeded {timeout}s",
        )
    except OSError as exc:
        return GateResult(
            gate_id,
            title,
            ERROR,
            True,
            f"gate error → fail closed: could not execute `{script}`: {exc.__class__.__name__}",
        )
    output = _scrub((proc.stdout or "") + (proc.stderr or ""), root)
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    if len(lines) > MAX_OUTPUT_LINES:
        lines = lines[:MAX_OUTPUT_LINES] + [f"… {len(lines) - MAX_OUTPUT_LINES} more lines omitted"]
    command = " ".join(["python3", script, *fixed_args, *paths])
    if proc.returncode == 0:
        return GateResult(gate_id, title, PASS, True, f"`{command}` exited 0", lines)
    return GateResult(
        gate_id,
        title,
        FAIL,
        True,
        f"`{command}` exited {proc.returncode}",
        lines or ["(tool produced no output)"],
    )


# ---------------------------------------------------------------------------
# internal gates
# ---------------------------------------------------------------------------


def _load_gate(loaded: LoadResult) -> GateResult:
    if loaded.errors:
        return GateResult(
            "load",
            "Documents load",
            FAIL,
            True,
            f"{len(loaded.errors)} file(s) failed to load",
            [f"`{e.path}`: {e.message}" for e in loaded.errors],
        )
    return GateResult(
        "load",
        "Documents load",
        PASS,
        True,
        f"{len(loaded.files)} file(s), {len(loaded.entries)} entr{'y' if len(loaded.entries) == 1 else 'ies'}",
    )


def _integrity_gate(loaded: LoadResult, policy: Mapping[str, Any]) -> GateResult:
    data = _integrity.check_integrity(loaded, policy)
    details = [
        f"{f['level']} `{f['code']}` {f.get('entry', {}).get('location', '')}: {f['message']}".replace(
            "  ", " "
        )
        for f in data["findings"]
    ]
    errors, warnings = data["counts"]["errors"], data["counts"]["warnings"]
    if errors:
        return GateResult(
            "integrity", "Id integrity", FAIL, True, f"{errors} error(s), {warnings} warning(s)", details, data
        )
    if warnings:
        return GateResult(
            "integrity", "Id integrity", WARN, True, f"0 errors, {warnings} warning(s)", details, data
        )
    return GateResult("integrity", "Id integrity", PASS, True, "no findings", [], data)


def _duplicates_gate(loaded: LoadResult, policy: Mapping[str, Any]) -> GateResult:
    data = _dedup.find_duplicates(loaded, policy)
    details: list[str] = []
    for rec in data["exact"]:
        details.append(
            f"exact: `{rec['a']['uuid']}` ↔ `{rec['b']['uuid']}` — {rec['exact_reason']} "
            f"(matched: {', '.join(rec['matched_fields']) or 'none'})"
        )
    for rec in data["near"]:
        details.append(
            f"near {rec['score']:.4f}: `{rec['a']['uuid']}` ↔ `{rec['b']['uuid']}` "
            f"(matched: {', '.join(rec['matched_fields']) or 'none'})"
        )
    if details:
        details.append("Humans decide; merge with `lifecycle.supersedes`. The bot never merges.")
    exact, near = data["counts"]["exact"], data["counts"]["near"]
    summary = f"{exact} exact, {near} near (threshold {data['near_threshold']})"
    if exact:
        return GateResult("duplicates", "Exact and near duplicates", FAIL, True, summary, details, data)
    if near:
        return GateResult("duplicates", "Exact and near duplicates", WARN, True, summary, details, data)
    return GateResult("duplicates", "Exact and near duplicates", PASS, True, summary, [], data)


def _conflicts_gate(
    loaded: LoadResult,
    policy: Mapping[str, Any],
    as_of: str,
    asserted: Iterable[tuple[str, str]],
) -> GateResult:
    data = _conflicts.find_conflicts(loaded, policy, as_of, asserted)
    details: list[str] = []
    for rec in data["conflicts"]:
        flag = "blocking" if rec["blocking"] else "advisory"
        details.append(
            f"{flag} ({rec['source']}): `{rec['a']['uuid']}` ↔ `{rec['b']['uuid']}` — "
            f"undeclared: {', '.join(rec['undeclared_dimensions'])}"
        )
    for rec in data["unattributable"]:
        flag = "blocking" if rec.get("blocking") else "advisory"
        details.append(
            f"{flag} unattributable ({rec['source']}): `{rec['a']['uuid']}` ↔ `{rec['b']['uuid']}` — "
            f"{rec['reason']}"
        )
    for rec in data["existing_records"]:
        if rec["state"] != "resolved":
            flag = "blocking" if rec["blocking"] else "advisory"
            details.append(
                f"{flag} recorded on `{rec['entry']['uuid']}` with `{rec['with']}`: "
                f"{rec['state']} — {rec['reason']}"
            )
    if details:
        details.append(
            "Both entries keep their place; refine the undeclared dimensions on each. "
            "The bot records, it does not arbitrate."
        )
    c = data["counts"]
    summary = (
        f"{c['conflicts']} new, {c['unattributable']} unattributable, "
        f"{c['existing_unresolved']} recorded-unresolved, {c['blocking']} blocking"
    )
    if c["blocking"]:
        return GateResult("conflicts", "Coordinate conflicts", FAIL, True, summary, details, data)
    if c["conflicts"] or c["unattributable"] or c["existing_unresolved"]:
        return GateResult("conflicts", "Coordinate conflicts", WARN, True, summary, details, data)
    return GateResult("conflicts", "Coordinate conflicts", PASS, True, summary, [], data)


def _staleness_gate(
    loaded: LoadResult, policy: Mapping[str, Any], as_of: _dt.date, blocking: bool
) -> GateResult:
    data = _staleness.sweep(loaded, policy, as_of)
    details = [
        f"propose `{p['entry']['uuid']}` verified → stale: {', '.join(p['reasons'])} "
        f"(last_verified_at {p['last_verified_at']}, {p['age_days']} days)"
        for p in data["proposals"]
    ]
    details += [
        f"cannot order `{u['entry']['uuid']}`: {', '.join(u['notes'])}" for u in data["unorderable"]
    ]
    if details:
        details.append("Proposals only; `corpus/verified/` is changed by reviewed PRs, never by the bot.")
    c = data["counts"]
    summary = (
        f"{c['proposals']} downgrade proposal(s), {c['already_stale']} already stale, "
        f"as of {data['as_of']}"
    )
    title = "Staleness sweep"
    if c["proposals"] or c["unorderable"]:
        status = FAIL if blocking else WARN
        return GateResult("staleness", title, status, blocking, summary, details, data)
    return GateResult("staleness", title, PASS, blocking, summary, [], data)


def _skipped(gate_id: str, title: str, reason: str) -> GateResult:
    return GateResult(gate_id, title, SKIPPED, True, f"skipped → fail closed: {reason}")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_gates(
    paths: Sequence[str],
    *,
    mode: str = "pr",
    root: Optional[Path] = None,
    policy_path: Optional[str] = None,
    as_of: Optional[str] = None,
    asserted_path: Optional[str] = None,
    python: str = sys.executable,
) -> dict[str, Any]:
    """Run every gate; never raises for gate failures, only for bot misuse."""
    if mode not in ("pr", "audit"):
        raise ValueError("mode must be 'pr' or 'audit'")
    root = root or repo_root()
    rel_paths = [relpath(root / p if not Path(p).is_absolute() else Path(p), root) for p in paths]
    as_of_date = _staleness.parse_date(as_of) if as_of else _staleness.today_utc()
    if as_of_date is None:
        raise ValueError("as_of must be YYYY-MM-DD")

    results: list[GateResult] = []
    policy: Optional[Mapping[str, Any]] = None
    policy_error: Optional[str] = None
    try:
        policy = load_policy(policy_path)
    except PolicyError as exc:
        policy_error = str(exc)

    loaded = load_paths(rel_paths, root)
    results.append(_load_gate(loaded))

    for gate_id, title, script, fixed in EXTERNAL_GATES:
        if mode == "audit" and gate_id == "schema":
            # The audit re-scans what already landed; schema is a PR-time gate.
            continue
        results.append(run_external_gate(gate_id, title, script, fixed, rel_paths, root, python=python))

    asserted: set[tuple[str, str]] = set()
    asserted_error: Optional[str] = None
    try:
        asserted = _conflicts.load_asserted_pairs(asserted_path)
    except (OSError, ValueError) as exc:
        asserted_error = str(exc)

    if policy_error:
        reason = f"policy error: {policy_error}"
        results += [
            _skipped("integrity", "Id integrity", reason),
            _skipped("duplicates", "Exact and near duplicates", reason),
            _skipped("conflicts", "Coordinate conflicts", reason),
            _skipped("staleness", "Staleness sweep", reason),
        ]
    elif loaded.errors:
        reason = "documents did not load"
        results += [
            _skipped("integrity", "Id integrity", reason),
            _skipped("duplicates", "Exact and near duplicates", reason),
            _skipped("conflicts", "Coordinate conflicts", reason),
            _skipped("staleness", "Staleness sweep", reason),
        ]
    else:
        assert policy is not None
        results.append(_integrity_gate(loaded, policy))
        results.append(_duplicates_gate(loaded, policy))
        if asserted_error:
            results.append(
                GateResult(
                    "conflicts",
                    "Coordinate conflicts",
                    ERROR,
                    True,
                    f"gate error → fail closed: asserted pairs file is invalid: {asserted_error}",
                )
            )
        else:
            results.append(_conflicts_gate(loaded, policy, as_of_date.isoformat(), asserted))
        results.append(_staleness_gate(loaded, policy, as_of_date, blocking=(mode == "audit")))

    failing = [r for r in results if r.blocking and not r.ok]
    warnings = [r for r in results if r.status == WARN]
    return {
        "bot": "vaws-knowledge-review-bot",
        "report_version": 1,
        "mode": mode,
        "paths": rel_paths,
        "as_of": as_of_date.isoformat(),
        "overall": FAIL if failing else PASS,
        "permits": (
            "corpus/unverified/ only (bot approval never establishes truth)"
            if not failing
            else "nothing"
        ),
        "counts": {
            "gates": len(results),
            "passed": sum(1 for r in results if r.status == PASS),
            "warned": len(warnings),
            "failed": len(failing),
        },
        "gates": [r.as_dict() for r in results],
    }


__all__ = [
    "ERROR",
    "FAIL",
    "GateResult",
    "PASS",
    "SKIPPED",
    "UNAVAILABLE",
    "WARN",
    "DependencyError",
    "run_external_gate",
    "run_gates",
]
