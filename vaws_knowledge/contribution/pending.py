"""Small recoverable contribution records keyed by the stable public path."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vaws_knowledge.contribution.documents import digest_token, require_kind, require_public_relpath, safe_file_path
from vaws_knowledge.contribution.errors import IdentityError
from vaws_knowledge.local.instance import InstanceLock

SCHEMA = "vaws-knowledge-contribution-pending/v2"
PENDING_STATUSES = (
    "pending", "awaiting_transport", "blocked_redaction", "submitted", "pr_open", "merged", "closed",
)
STATUS_PENDING = "pending"
STATUS_AWAITING = "awaiting_transport"
STATUS_BLOCKED = "blocked_redaction"
STATUS_SUBMITTED = "submitted"
STATUS_PR_OPEN = "pr_open"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pending_dir(state_root: Path) -> Path:
    return Path(state_root) / "contribution" / "pending"


def pending_lock(state_root: Path) -> InstanceLock:
    # This lock protects local read/replace only, never git or network calls.
    return InstanceLock(Path(state_root) / "contribution" / "pending.lock")


def candidate_key(candidate_path: Path) -> str:
    canonical = os.path.normcase(str(Path(candidate_path).resolve()))
    return hashlib.sha256(os.fsencode(canonical)).hexdigest()


def pending_path(state_root: Path, public_relpath: str, kind: str = "knowledge") -> Path:
    require_public_relpath(public_relpath, kind)
    token = hashlib.sha256(public_relpath.encode("utf-8")).hexdigest()
    return safe_file_path(pending_dir(state_root), f"{kind}/{token}.json")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class PendingRecord:
    content_digest: str
    title: str
    public_relpath: str
    status: str = STATUS_PENDING
    branch: str = ""
    candidate_relpath: str | None = None
    candidate_keys: list[str] = field(default_factory=list)
    submitted_digest: str | None = None
    requires_existing: bool = False
    explicit_path: bool = False
    revision: int = 0
    pr_number: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)
    kind: str = "knowledge"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = SCHEMA
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PendingRecord":
        if payload.get("schema") != SCHEMA:
            raise IdentityError("unknown pending record schema")
        digest = str(payload.get("content_digest") or "")
        digest_token(digest)
        status = str(payload.get("status") or STATUS_PENDING)
        if status not in PENDING_STATUSES:
            raise IdentityError("unknown pending status")
        pr_number = payload.get("pr_number")
        if pr_number is not None and (not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0):
            raise IdentityError("malformed pr_number")
        kind = require_kind(str(payload.get("kind") or "knowledge"))
        relpath = require_public_relpath(str(payload.get("public_relpath") or ""), kind)
        revision = payload.get("revision", 0)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise IdentityError("malformed pending revision")
        submitted = payload.get("submitted_digest")
        if submitted is not None:
            digest_token(submitted)
        return cls(
            content_digest=digest,
            title=str(payload.get("title") or ""),
            public_relpath=relpath,
            status=status,
            branch=str(payload.get("branch") or ""),
            candidate_relpath=payload.get("candidate_relpath") if isinstance(payload.get("candidate_relpath"), str) else None,
            candidate_keys=[key for key in payload.get("candidate_keys", []) if isinstance(key, str)],
            submitted_digest=submitted,
            explicit_path=payload.get("explicit_path") is True,
            requires_existing=payload.get("requires_existing") is True,
            revision=revision,
            pr_number=pr_number,
            pr_url=payload.get("pr_url") if isinstance(payload.get("pr_url"), str) else None,
            head_sha=payload.get("head_sha") if isinstance(payload.get("head_sha"), str) else None,
            last_error=payload.get("last_error") if isinstance(payload.get("last_error"), str) else None,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            notes=list(payload.get("notes") or []),
            kind=kind,
        )


def save_pending_unlocked(state_root: Path, record: PendingRecord) -> PendingRecord:
    """Write while holding ``pending_lock``; callers must use a fresh record."""

    record.updated_at = utc_now()
    if not record.created_at:
        record.created_at = record.updated_at
    record.revision += 1
    _atomic_write_json(pending_path(state_root, record.public_relpath, record.kind), record.to_dict())
    return record


def save_pending(state_root: Path, record: PendingRecord, *, expected_revision: int | None = None) -> PendingRecord:
    """Compare-and-save; stale status updates return the current record unchanged."""

    with pending_lock(state_root):
        current = load_pending(state_root, record.public_relpath, record.kind)
        expected = record.revision if expected_revision is None else expected_revision
        if current is not None and current.revision != expected:
            return current
        return save_pending_unlocked(state_root, record)


def load_pending(state_root: Path, public_relpath: str, kind: str = "knowledge") -> PendingRecord | None:
    path = pending_path(state_root, public_relpath, kind)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    record = PendingRecord.from_dict(payload)
    return record if record.kind == kind and record.public_relpath == public_relpath else None


def iter_pending(state_root: Path) -> list[PendingRecord]:
    root = pending_dir(state_root)
    if not root.is_dir():
        return []
    records: list[PendingRecord] = []
    for path in sorted(root.glob("*/*.json")):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                continue
            record = PendingRecord.from_dict(payload)
            if path == pending_path(state_root, record.public_relpath, record.kind):
                records.append(record)
        except (IdentityError, OSError, KeyError, TypeError, ValueError):
            continue
    return records
