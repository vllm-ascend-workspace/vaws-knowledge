"""The write path: candidate layer only.

Capture exists for one moment -- a developer has just fixed something and the
diagnosis is still in their head. It writes to the `candidate` layer and
nowhere else:

* `shared` is `corpus/verified/` in this repo. docs/federation.md says a fork
  never writes into `verified/`; promotion happens through a reviewed PR with
  a followable evidence reference and a non-submitter confirmation.
* `project` is a business repo's tracked knowledge. It changes through that
  repo's own PRs, not through a background service call.

So any layer other than `candidate` is refused, not silently redirected. A
redirect would be worse than an error: the caller would believe it had
recorded a reviewed fact.

Canonicalization
----------------
`content_hash` is the revision key for federated sync, so it must be computed
identically everywhere. `docs/federation.md` specifies it exactly. This module
prefers to shell out to `python3 tools/canonical.py` when that module exists,
and falls back to a local implementation of the same specification when it
does not. The fallback is a deliberate duplication -- see server/README.md --
and it cross-checks against the tool whenever the tool is available, so a
divergence surfaces as a warning instead of as a sync storm.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid as _uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .layers import (
    WRITABLE_LAYERS,
    ServiceConfig,
    load_config,
    repo_root,
    yaml,
)
from .query import SCOPE_DIMENSIONS

ENTRY_FIELDS = {
    "uuid",
    "slug",
    "content_hash",
    "status",
    "confidence",
    "scope",
    "provenance",
    "verification",
    "lifecycle",
    "conflicts",
    "rule",
}
RULE_FIELDS = {"summary", "symptom", "root_cause", "resolution", "avoidance", "fingerprints"}
RULE_REQUIRED = ("summary", "symptom", "root_cause", "resolution")
STATUSES = ("verified", "unverified", "stale", "deprecated", "resolved")
CONFIDENCES = ("high", "medium", "low")
EVIDENCE_TYPES = ("run_manifest", "pull_request", "issue", "commit", "ci_run")

KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
_WS_RE = re.compile(r"\s+")

#: argv shapes tried against tools/canonical.py, entry JSON on stdin. The
#: sibling module's CLI is not fixed yet, so this probes rather than assumes.
_CANONICAL_ARGV_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("--content-hash", "-"),
    ("--hash", "-"),
    ("hash", "-"),
    ("-",),
)


class CaptureRefused(Exception):
    """The requested write is not allowed at all (wrong layer, read-only mount)."""

    def __init__(self, message: str, *, layer: str | None = None):
        super().__init__(message)
        self.layer = layer


class CaptureRejected(Exception):
    """The entry itself is not well formed enough to store."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# --------------------------------------------------------------------------
# canonicalization (docs/federation.md)
# --------------------------------------------------------------------------


def _normalize_string(value: str) -> str:
    """Step 3: strip ends, normalize line endings to LF, do not reflow."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _normalize_fingerprints(items: Any) -> list[str]:
    """Step 2: lowercase, trim, collapse whitespace, drop dupes/empties, sort."""

    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, Mapping)):
        return []
    seen: set[str] = set()
    for item in items:
        text = _WS_RE.sub(" ", str(item).strip().lower())
        if text:
            seen.add(text)
    return sorted(seen)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    return value


def canonical_payload(entry: Mapping[str, Any]) -> str:
    """Canonical JSON for an entry, per docs/federation.md steps 1-4."""

    rule_in = entry.get("rule") if isinstance(entry.get("rule"), Mapping) else {}
    scope_in = entry.get("scope") if isinstance(entry.get("scope"), Mapping) else {}

    rule: dict[str, Any] = {}
    for key, value in rule_in.items():
        if key == "fingerprints":
            rule[str(key)] = _normalize_fingerprints(value)
        else:
            rule[str(key)] = _normalize_value(value)

    scope = {str(k): _normalize_value(v) for k, v in scope_in.items()}

    return json.dumps(
        {"rule": rule, "scope": scope},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def builtin_content_hash(entry: Mapping[str, Any]) -> str:
    """Step 5. Local implementation of the specified canonicalization."""

    payload = canonical_payload(entry)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_tool_path(root: Path | None = None) -> Path | None:
    path = (root or repo_root()) / "tools" / "canonical.py"
    return path if path.is_file() else None


def tool_content_hash(entry: Mapping[str, Any], root: Path | None = None) -> tuple[str | None, dict[str, Any]]:
    """Best-effort ``python3 tools/canonical.py`` invocation.

    Returns ``(hash_or_none, diagnostics)``. Never raises: a missing or
    differently-shaped tool falls back to the builtin implementation.
    """

    tool = _canonical_tool_path(root)
    info: dict[str, Any] = {"tool_present": tool is not None}
    if tool is None:
        info["reason"] = "tools/canonical.py not present in this checkout"
        return None, info

    entry_json = json.dumps(dict(entry), ensure_ascii=False)
    attempts: list[dict[str, Any]] = []
    for argv in _CANONICAL_ARGV_CANDIDATES:
        cmd = [sys.executable or "python3", str(tool), *argv]
        try:
            proc = subprocess.run(
                cmd,
                input=entry_json,
                capture_output=True,
                text=True,
                timeout=20,
                cwd=str(tool.parent.parent),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            attempts.append({"argv": list(argv), "error": f"{type(exc).__name__}: {exc}"})
            continue
        found = HASH_RE.search(proc.stdout or "")
        if proc.returncode == 0 and found:
            info["argv"] = list(argv)
            info["attempts"] = attempts
            return found.group(0), info
        attempts.append(
            {
                "argv": list(argv),
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:400],
            }
        )
    info["attempts"] = attempts
    info["reason"] = "tools/canonical.py present but no probed CLI shape produced a sha256 hash"
    return None, info


def content_hash(entry: Mapping[str, Any], root: Path | None = None) -> tuple[str, dict[str, Any]]:
    """Compute ``content_hash``, preferring the shared tool when usable."""

    builtin = builtin_content_hash(entry)
    from_tool, info = tool_content_hash(entry, root)
    diagnostics: dict[str, Any] = {"builtin": builtin, "tool": info}
    if from_tool is None:
        diagnostics["source"] = "server-builtin"
        return builtin, diagnostics
    diagnostics["source"] = "tools/canonical.py"
    diagnostics["tool_hash"] = from_tool
    if from_tool != builtin:
        diagnostics["disagreement"] = (
            "tools/canonical.py and the server's fallback canonicalization "
            "disagree on this entry's content_hash. The tool's value was used. "
            "One of the two implementations does not follow docs/federation.md."
        )
    return from_tool, diagnostics


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _check_constraint(dimension: str, constraint: Any, problems: list[str]) -> None:
    where = f"scope.{dimension}"
    if not isinstance(constraint, Mapping):
        problems.append(f"{where}: must be a mapping")
        return
    keys = set(constraint)
    if keys == {"any", "basis"}:
        if constraint.get("any") is not True:
            problems.append(f"{where}.any must be literally true")
        basis = constraint.get("basis")
        if not isinstance(basis, str) or len(basis.strip()) < 12:
            problems.append(
                f"{where}.basis must be at least 12 characters: an independence "
                "claim is reviewed as a claim, not as prose"
            )
        return
    if keys == {"values"}:
        values = constraint.get("values")
        if not isinstance(values, (list, tuple)) or not values:
            problems.append(f"{where}.values must be a non-empty list")
            return
        for value in values:
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}.values entries must be non-empty strings")
        return
    if keys == {"range"}:
        rng = constraint.get("range")
        if not isinstance(rng, Mapping) or set(rng) != {"min", "max"}:
            problems.append(
                f"{where}.range must have exactly min and max; unbounded is an "
                "explicit null, not a missing key"
            )
            return
        for bound in ("min", "max"):
            value = rng.get(bound)
            if value is None or isinstance(value, str):
                continue
            problems.append(
                f"{where}.range.{bound} must be a string or null (got "
                f"{type(value).__name__}; YAML 2.5 is a float and compares wrong)"
            )
        return
    problems.append(
        f"{where}: must be exactly one of {{any,basis}}, {{values}} or {{range}}, got {sorted(keys)}"
    )


def validate_entry(entry: Mapping[str, Any], *, kind: str) -> list[str]:
    """Structural check mirroring schemas/knowledge-v2.schema.json.

    This is not a substitute for the schema. It exists because `jsonschema` is
    not guaranteed to be installed and because capture must not write a
    document that the review bot will reject. When `jsonschema` *is* importable
    the caller also runs the real schema (see :func:`schema_validate`).
    """

    problems: list[str] = []

    if not KIND_RE.match(str(kind)):
        problems.append(f"kind {kind!r} does not match ^[a-z0-9][a-z0-9-]{{0,63}}$")

    unknown = set(entry) - ENTRY_FIELDS
    if unknown:
        problems.append(
            "undeclared entry field(s) "
            + ", ".join(sorted(unknown))
            + ": the schema is the egress whitelist, so an undeclared field cannot be stored"
        )

    slug = entry.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        problems.append("slug must match ^[a-z0-9][a-z0-9._-]{0,127}$")

    status = entry.get("status")
    if status not in STATUSES:
        problems.append(f"status must be one of {list(STATUSES)}")
    confidence = entry.get("confidence")
    if confidence not in CONFIDENCES:
        problems.append(f"confidence must be one of {list(CONFIDENCES)}")
    if confidence == "high" and status not in ("verified", "stale", "resolved"):
        problems.append(
            "confidence high is reserved for verified/stale/resolved: writing "
            "confidently is not evidence"
        )

    rule = entry.get("rule")
    if not isinstance(rule, Mapping):
        problems.append("rule must be a mapping")
    else:
        for field_name in RULE_REQUIRED:
            value = rule.get(field_name)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"rule.{field_name} is required and must be a non-empty string")
        extra = set(rule) - RULE_FIELDS
        if extra:
            problems.append("undeclared rule field(s) " + ", ".join(sorted(extra)))
        fingerprints = rule.get("fingerprints")
        if fingerprints is not None and (
            not isinstance(fingerprints, (list, tuple))
            or any(not isinstance(f, str) or not f.strip() for f in fingerprints)
        ):
            problems.append("rule.fingerprints must be a list of non-empty strings")

    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        problems.append(
            "scope must be a mapping declaring all twelve dimensions: "
            + ", ".join(SCOPE_DIMENSIONS)
        )
    else:
        missing = [d for d in SCOPE_DIMENSIONS if d not in scope]
        if missing:
            problems.append(
                "scope is missing " + ", ".join(missing) + ": there is no omitted state, so "
                "each dimension must be bounded (values/range) or claim independence (any+basis)"
            )
        undeclared = set(scope) - set(SCOPE_DIMENSIONS)
        if undeclared:
            problems.append("undeclared scope dimension(s) " + ", ".join(sorted(undeclared)))
        for dimension in SCOPE_DIMENSIONS:
            if dimension in scope:
                _check_constraint(dimension, scope[dimension], problems)

    provenance = entry.get("provenance")
    if not isinstance(provenance, Mapping):
        problems.append("provenance must be a mapping")
    else:
        for field_name in ("contributor", "origin_repo", "submitted_at", "redaction_profile"):
            if not str(provenance.get(field_name) or "").strip():
                problems.append(f"provenance.{field_name} is required")
        if not DATE_RE.match(str(provenance.get("submitted_at", ""))):
            problems.append("provenance.submitted_at must be YYYY-MM-DD")
        if not re.match(r"^r[0-9]+$", str(provenance.get("redaction_profile", ""))):
            problems.append("provenance.redaction_profile must match ^r[0-9]+$")

    lifecycle = entry.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        problems.append("lifecycle must be a mapping")
    else:
        for field_name in ("first_seen", "updated_at"):
            if not DATE_RE.match(str(lifecycle.get(field_name, ""))):
                problems.append(f"lifecycle.{field_name} must be YYYY-MM-DD")

    if status in ("verified", "stale"):
        verification = entry.get("verification")
        if not isinstance(verification, Mapping):
            problems.append(f"status {status} requires a verification record")
        else:
            evidence = verification.get("evidence")
            if not isinstance(evidence, (list, tuple)) or not evidence:
                problems.append(f"status {status} requires at least one evidence reference")
            else:
                for item in evidence:
                    if not isinstance(item, Mapping):
                        problems.append("evidence must be references, not prose")
                        continue
                    if item.get("type") not in EVIDENCE_TYPES:
                        problems.append(f"evidence.type must be one of {list(EVIDENCE_TYPES)}")
                    if not str(item.get("ref") or "").strip():
                        problems.append("evidence.ref is required")
            verified_by = verification.get("verified_by")
            if not isinstance(verified_by, (list, tuple)) or not verified_by:
                problems.append(f"status {status} requires verified_by to name a confirmer")
            against = verification.get("verified_against")
            if not isinstance(against, Mapping):
                problems.append(f"status {status} requires verification.verified_against")
            else:
                for field_name in (
                    "soc",
                    "cann",
                    "driver",
                    "torch",
                    "torch_npu",
                    "vllm",
                    "vllm_ascend",
                ):
                    if not str(against.get(field_name) or "").strip():
                        problems.append(f"verification.verified_against.{field_name} is required")
            if not DATE_RE.match(str(verification.get("last_verified_at", ""))):
                problems.append("verification.last_verified_at must be YYYY-MM-DD")

    if status == "resolved":
        lifecycle = entry.get("lifecycle") or {}
        resolved_by = lifecycle.get("resolved_by") if isinstance(lifecycle, Mapping) else None
        if not isinstance(resolved_by, Mapping) or not str(resolved_by.get("ref") or "").strip():
            problems.append(
                "status resolved requires lifecycle.resolved_by pointing at the fix"
            )

    return problems


def schema_validate(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate against the real schema when jsonschema is importable."""

    try:
        from jsonschema import Draft202012Validator  # type: ignore
    except ImportError:
        return {
            "ran": False,
            "reason": (
                "jsonschema is not installed, so only the server's structural check ran. "
                "Install it with: python3 -m pip install -r requirements.txt"
            ),
        }
    schema_path = repo_root() / "schemas" / "knowledge-v2.schema.json"
    if not schema_path.is_file():
        return {"ran": False, "reason": f"schema not found at {schema_path.name}"}
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    errors = [
        f"{list(e.path)}: {e.message}"
        for e in sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    ]
    return {"ran": True, "errors": errors, "valid": not errors}


# --------------------------------------------------------------------------
# the write
# --------------------------------------------------------------------------


def _document_for(kind: str, today: str) -> dict[str, Any]:
    # schemas/knowledge-v2.schema.json only knows layer=verified|unverified --
    # the corpus review zones. The trust layer (shared/project/candidate) comes
    # from the mount, not from the file. A candidate capture is by definition
    # unreviewed, so the document layer is 'unverified'.
    return {
        "schema_version": 2,
        "kind": kind,
        "layer": "unverified",
        "updated_at": today,
        "entries": [],
    }


def _dump_document(document: Mapping[str, Any], path: Path) -> str:
    if path.suffix == ".json" or yaml is None:
        return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    return yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True, width=100)


def _load_document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        if yaml is None:
            raise CaptureRefused(
                "PyYAML is not importable, so an existing YAML candidate file cannot be "
                "read safely. Install it with: python3 -m pip install -r server/requirements.txt"
            )
        data = yaml.safe_load(text)
    return data if isinstance(data, dict) else None


def candidate_root(config: ServiceConfig) -> Path:
    mount = config.mount("candidate")
    if mount.read_only:
        raise CaptureRefused(
            "the candidate mount is configured read_only; capture has nowhere to write",
            layer="candidate",
        )
    if not mount.roots:
        raise CaptureRefused(
            "no candidate root is configured. Set layers.candidate.root in the config "
            "file or VAWS_KNOWLEDGE_CANDIDATE_ROOT in the environment.",
            layer="candidate",
        )
    return mount.roots[0]


def capture(
    entry: Mapping[str, Any],
    *,
    kind: str = "known-failure-signatures",
    layer: str = "candidate",
    config: ServiceConfig | None = None,
    today: _dt.date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write one entry into the candidate layer.

    Raises :class:`CaptureRefused` for any layer other than `candidate`, and
    :class:`CaptureRejected` when the entry would not satisfy schema v2.
    """

    if layer != "candidate":
        known = "shared/project are review-gated; " if layer in ("shared", "project") else ""
        raise CaptureRefused(
            f"refusing to write into layer {layer!r}: capture only ever writes the "
            f"candidate layer. {known}"
            "promotion happens through a reviewed PR with a followable evidence "
            "reference and a non-submitter confirmation (see docs/federation.md).",
            layer=layer,
        )
    assert layer in WRITABLE_LAYERS  # guarded above; kept as an invariant

    config = config or load_config()
    date = today or _dt.date.today()
    stamp = date.isoformat()

    if not isinstance(entry, Mapping):
        raise CaptureRejected(["entry must be a mapping"])

    kind = str(entry.get("kind") or kind)
    draft: dict[str, Any] = {k: v for k, v in entry.items() if k != "kind"}

    draft.setdefault("uuid", str(_uuid.uuid4()))
    draft.setdefault("status", "unverified")
    draft.setdefault("confidence", "low" if draft["status"] == "unverified" else "medium")

    provenance = dict(draft.get("provenance") or {})
    provenance.setdefault("contributor", config.identity.get("contributor", "anonymous"))
    provenance.setdefault("origin_repo", config.identity.get("origin_repo", "local/unpublished"))
    provenance.setdefault("redaction_profile", config.identity.get("redaction_profile", "r1"))
    provenance["submitted_at"] = provenance.get("submitted_at") or stamp
    draft["provenance"] = provenance

    lifecycle = dict(draft.get("lifecycle") or {})
    lifecycle.setdefault("first_seen", stamp)
    lifecycle["updated_at"] = stamp
    draft["lifecycle"] = lifecycle

    warnings: list[str] = []

    rule = draft.get("rule")
    if isinstance(rule, Mapping) and rule.get("fingerprints") is not None:
        # Store fingerprints in their canonical form. The hash normalizes them
        # anyway (docs/federation.md step 2); storing the raw list would mean
        # nobody could reproduce content_hash from the file they are reading.
        rule = dict(rule)
        normalized = _normalize_fingerprints(rule.get("fingerprints"))
        if normalized != list(rule.get("fingerprints") or []):
            warnings.append(
                "rule.fingerprints were normalized (lowercased, whitespace collapsed, "
                "deduplicated, sorted) so the stored form matches the hashed form"
            )
        rule["fingerprints"] = normalized
        draft["rule"] = rule

    if draft["status"] != "unverified":
        warnings.append(
            f"status={draft['status']} in the candidate layer is a local claim only. "
            "The shared corpus grants that status through review, not through capture."
        )

    hash_value, hash_info = content_hash(draft, repo_root())
    supplied_hash = draft.get("content_hash")
    if supplied_hash and supplied_hash != hash_value:
        warnings.append(
            "supplied content_hash did not match the canonicalization in "
            "docs/federation.md and was replaced; content_hash is a revision key, "
            "not a free-form field"
        )
    draft["content_hash"] = hash_value
    if hash_info.get("disagreement"):
        warnings.append(hash_info["disagreement"])

    problems = validate_entry(draft, kind=kind)
    if problems:
        raise CaptureRejected(problems)

    root = candidate_root(config)
    suffix = ".yaml" if yaml is not None else ".json"
    path = root / f"{kind}{suffix}"
    if not path.is_file():
        # Do not create a second file for a kind already stored in the other
        # serialization; a duplicate uuid across two files is a sync hazard.
        other = root / f"{kind}{'.json' if suffix == '.yaml' else '.yaml'}"
        if other.is_file():
            path = other

    try:
        document = _load_document(path) or _document_for(kind, stamp)
    except CaptureRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - never clobber a file we cannot read
        raise CaptureRefused(
            f"existing candidate file {path.name} cannot be parsed ({type(exc).__name__}: "
            f"{exc}); refusing to overwrite it. Fix or move the file, then retry.",
            layer="candidate",
        ) from exc
    document.setdefault("schema_version", 2)
    document.setdefault("kind", kind)
    document.setdefault("layer", "unverified")
    entries = document.get("entries")
    if not isinstance(entries, list):
        entries = []
    document["entries"] = entries
    document["updated_at"] = stamp

    action = "created"
    for index, existing in enumerate(entries):
        if isinstance(existing, Mapping) and existing.get("uuid") == draft["uuid"]:
            if existing.get("content_hash") == draft["content_hash"]:
                action = "unchanged"
                # docs/federation.md: same uuid + same content_hash is a no-op.
                draft["lifecycle"]["updated_at"] = str(
                    (existing.get("lifecycle") or {}).get("updated_at") or stamp
                )
            else:
                action = "revised"
            entries[index] = draft
            break
    else:
        entries.append(draft)

    schema_report = schema_validate(document)
    if schema_report.get("ran") and schema_report.get("errors"):
        raise CaptureRejected(
            ["schema validation failed: " + err for err in schema_report["errors"]]
        )

    result: dict[str, Any] = {
        "ok": True,
        "layer": "candidate",
        "action": action,
        "uuid": draft["uuid"],
        "slug": draft.get("slug"),
        "kind": kind,
        "status": draft["status"],
        "confidence": draft["confidence"],
        "content_hash": draft["content_hash"],
        "content_hash_source": hash_info.get("source"),
        "file": str(path),
        "dry_run": dry_run,
        "schema_check": schema_report,
        "warnings": warnings,
        "notes": [
            "candidate entries are unreviewed and are returned by query only with "
            "include_unverified=true, labelled layer=candidate",
        ],
    }

    if dry_run or action == "unchanged":
        result["written"] = False
        if action == "unchanged":
            result["notes"].append(
                "same uuid and same content_hash: no revision, nothing rewritten"
            )
        return result

    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_dump_document(document, path), encoding="utf-8")
    os.replace(tmp, path)
    result["written"] = True
    return result
