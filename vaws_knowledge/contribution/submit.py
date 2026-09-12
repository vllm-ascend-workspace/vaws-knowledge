"""Prepare stable public documents and submit their latest revision to a PR."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path

from vaws_knowledge.contribution.documents import (
    DIGEST_PREFIX, MarkdownDocument, public_filename, require_kind,
    require_public_relpath, require_relative_path, safe_file_path,
)
from vaws_knowledge.contribution.errors import DocumentRejected, IdentityError, TransportError
from vaws_knowledge.contribution.gitops import commit_public_file, run_git
from vaws_knowledge.contribution.github import GitHubError, GitHubTransport, create_pull, get_pull
from vaws_knowledge.contribution.pending import (
    STATUS_AWAITING, STATUS_BLOCKED, STATUS_PR_OPEN, PendingRecord,
    candidate_key, iter_pending, load_pending, pending_lock, save_pending, save_pending_unlocked,
)
from vaws_knowledge.contribution.public import PublicCopy, prepare_public_copy, write_public_text


@dataclass(frozen=True)
class SubmitConfig:
    upstream: str
    fork: str
    default_branch: str = "main"
    knowledge_prefix: str = "corpus"
    push_remote: str | None = None


def _new_branch(kind: str) -> str:
    return f"contrib/{kind}/{secrets.token_hex(6)}"


def prepare_candidate(
    candidate_path: Path,
    *,
    state_root: Path,
    public_root: Path,
    kind: str = "knowledge",
    public_relpath: str | None = None,
) -> PendingRecord:
    """Prepare one revision; its public path is the durable document identity."""

    kind = require_kind(kind)
    if public_relpath is not None:
        require_public_relpath(public_relpath, kind)
    candidate_path = Path(candidate_path)
    key = candidate_key(candidate_path)
    existing_stat = candidate_path.stat()
    original = candidate_path.read_text(encoding="utf-8")
    copy = prepare_public_copy(original, kind=kind, public_relpath=public_relpath)
    after_stat = candidate_path.stat()
    if ((existing_stat.st_mtime_ns, existing_stat.st_size) != (after_stat.st_mtime_ns, after_stat.st_size)
            or candidate_path.read_text(encoding="utf-8") != original):
        raise RuntimeError("candidate file was mutated while preparing a public copy")
    if copy.blocked:
        try:
            parsed = MarkdownDocument.from_text(original)
            digest, title = parsed.digest, parsed.title
        except DocumentRejected:
            digest = DIGEST_PREFIX + hashlib.sha256(original.encode("utf-8")).hexdigest()
            title = ""
    else:
        digest, title = copy.document.digest, copy.document.title
    with pending_lock(state_root):
        records = [r for r in iter_pending(state_root) if r.kind == kind]
        if public_relpath is not None:
            existing = load_pending(state_root, public_relpath, kind)
        else:
            existing = next((r for r in records if key in r.candidate_keys), None)
        relative = (existing.public_relpath if existing is not None else public_relpath
                    or f"{kind}/{public_filename(title if not copy.blocked else 'contribution', kind)}")
        if (existing is None and public_relpath is None and kind == "knowledge"
                and (any(r.public_relpath == relative for r in records) or (Path(public_root) / relative).exists())):
            raise IdentityError("knowledge path is already occupied; specify public_relpath to revise that entry")
        destination = safe_file_path(public_root, relative)
        record = existing or PendingRecord(
            content_digest=digest, title=title, public_relpath=relative,
            branch=_new_branch(kind), candidate_relpath=candidate_path.name, candidate_keys=[key],
            requires_existing=public_relpath is not None and kind == "experience",
            explicit_path=public_relpath is not None,
            notes=["candidate left unchanged; public copy is redacted"], kind=kind,
        )
        association_changed = False
        if public_relpath is not None:
            # Explicit selection also associates this local source with the
            # chosen document. Unspecified captures never infer this link.
            if key not in record.candidate_keys:
                record.candidate_keys.append(key)
                association_changed = True
            if not record.explicit_path:
                record.explicit_path = True
                association_changed = True
            for previous in records:
                if previous.public_relpath == relative:
                    continue
                if key in previous.candidate_keys:
                    previous.candidate_keys = [item for item in previous.candidate_keys if item != key]
                    save_pending_unlocked(state_root, previous)
        changed = record.content_digest != digest
        if copy.blocked:
            record.content_digest = digest
            record.title = title
            record.status = STATUS_BLOCKED
            record.last_error = copy.reason or "redaction blocked public copy"
        else:
            write_public_text(destination, copy.text)
            record.content_digest = digest
            record.title = title
            if changed or record.status == STATUS_BLOCKED:
                record.status = "pending"
                record.last_error = None
            elif existing is not None and not association_changed:
                return record
        return save_pending_unlocked(state_root, record)


def _pr_body(record: PendingRecord) -> str:
    return (
        f"{record.title}\n\n"
        f"Content kind: {record.kind}\n\n"
        f"Public document: `{record.public_relpath}`\n"
        f"Public review does not prove hardware facts.\n"
    )


def _save_transport_result(state_root: Path, record: PendingRecord, *, initial_branch: str) -> PendingRecord:
    """Keep a concurrent capture's document, merging only this transport result."""

    with pending_lock(state_root):
        latest = load_pending(state_root, record.public_relpath, record.kind)
        if latest is None or latest.revision == record.revision:
            return save_pending_unlocked(state_root, record)
        if latest.branch not in {initial_branch, record.branch}:
            return latest  # a newer transport already began another PR cycle
        for field in ("branch", "pr_number", "pr_url", "head_sha", "submitted_digest", "requires_existing"):
            setattr(latest, field, getattr(record, field))
        if latest.status != STATUS_BLOCKED:
            latest.status = record.status if latest.content_digest == record.content_digest else "pending"
            latest.last_error = record.last_error
        return save_pending_unlocked(state_root, latest)


def submit_pending(
    record: PendingRecord,
    *,
    state_root: Path,
    public_root: Path,
    git_repo: Path,
    github: GitHubTransport,
    config: SubmitConfig,
) -> PendingRecord:
    """Submit a snapshot; any capture prepared during I/O remains pending."""

    with pending_lock(state_root):
        record = load_pending(state_root, record.public_relpath, record.kind) or record
        if record.status == STATUS_BLOCKED:
            return record
        if (record.status in {STATUS_PR_OPEN, "submitted", "merged", "closed"}
                and record.submitted_digest == record.content_digest):
            return record
        try:
            public_file = safe_file_path(public_root, require_public_relpath(record.public_relpath, record.kind))
            text = public_file.read_text(encoding="utf-8")
        except (IdentityError, OSError) as exc:
            record.status = STATUS_AWAITING
            record.last_error = "public copy is missing or unsafe: " + str(exc)
            return save_pending_unlocked(state_root, record)
        public_copy = prepare_public_copy(text, kind=record.kind, public_relpath=record.public_relpath)
        if public_copy.blocked or public_copy.document.digest != record.content_digest:
            record.status = STATUS_BLOCKED
            record.last_error = "public copy changed or failed redaction; prepare the candidate again"
            return save_pending_unlocked(state_root, record)
    initial_branch = record.branch
    try:
        prefix = config.knowledge_prefix
        if prefix:
            require_relative_path(prefix)
        repo_relpath = "/".join(part for part in (prefix, record.public_relpath) if part)
        if record.pr_number:
            pull = get_pull(github, upstream=config.upstream, number=record.pr_number)
            if pull["state"] == "closed":
                if record.submitted_digest == record.content_digest:
                    record.status = "merged" if pull.get("merged") else "closed"
                    return _save_transport_result(state_root, record, initial_branch=initial_branch)
                # A terminal PR's checkout must never be the base of a revision.
                record.branch = _new_branch(record.kind)
                record.pr_number = None
                record.pr_url = None
                record.head_sha = None
            elif record.submitted_digest == record.content_digest:
                record.status = STATUS_PR_OPEN
                record.last_error = None
                return _save_transport_result(state_root, record, initial_branch=initial_branch)
        if run_git(git_repo, ["status", "--porcelain"]).stdout.strip():
            raise TransportError("contribution checkout has uncommitted changes")
        start_ref = config.default_branch
        if config.push_remote:
            run_git(git_repo, ["fetch", "upstream", config.default_branch])
            start_ref = "FETCH_HEAD"
        if not record.pr_number:
            entry = run_git(git_repo, ["ls-tree", start_ref, "--", repo_relpath]).stdout.strip()
            if (record.kind == "knowledge" and not record.explicit_path
                    and record.submitted_digest is None and entry):
                raise TransportError("knowledge path is already occupied in the base; specify public_relpath to revise that entry")
            if record.requires_existing and (not entry or entry.split()[0] not in {"100644", "100755"}):
                raise TransportError("explicit public revision target does not exist as a regular file in the base")
            if entry and entry.split()[0] in {"100644", "100755"}:
                base_text = run_git(git_repo, ["show", f"{start_ref}:{repo_relpath}"]).stdout
                try:
                    base_digest = MarkdownDocument.from_text(base_text).digest
                except DocumentRejected:
                    base_digest = None
                if base_digest == record.content_digest:
                    record.status = "submitted"
                    record.submitted_digest = record.content_digest
                    record.requires_existing = False
                    record.last_error = None
                    return _save_transport_result(state_root, record, initial_branch=initial_branch)
        record.branch = record.branch or _new_branch(record.kind)
        head = commit_public_file(
            git_repo, branch=record.branch, relpath=repo_relpath, content=text,
            message=f"Contribute: {record.title}", start_ref=start_ref,
            require_existing=record.requires_existing,
        )
        if config.push_remote:
            run_git(git_repo, ["push", config.push_remote, f"HEAD:refs/heads/{record.branch}"])
        pull = create_pull(
            github, upstream=config.upstream, fork=config.fork, branch=record.branch,
            base=config.default_branch, title=record.title, body=_pr_body(record),
        )
    except (TransportError, GitHubError, IdentityError, OSError) as exc:
        if isinstance(exc, GitHubError):
            from vaws_knowledge.contribution.github import transport_message
            record.last_error = transport_message(exc)
        else:
            record.last_error = str(exc)
        record.status = STATUS_AWAITING
        return _save_transport_result(state_root, record, initial_branch=initial_branch)
    record.head_sha = head
    record.pr_number = int(pull["number"])
    html = pull.get("html_url")
    record.pr_url = html if isinstance(html, str) else None
    record.submitted_digest = record.content_digest
    record.requires_existing = False
    record.status = STATUS_PR_OPEN
    record.last_error = None
    return _save_transport_result(state_root, record, initial_branch=initial_branch)


def after_capture(
    candidate_path: Path,
    *,
    state_root: Path,
    public_root: Path,
    git_repo: Path | None = None,
    github: GitHubTransport | None = None,
    config: SubmitConfig | None = None,
    kind: str = "knowledge",
    public_relpath: str | None = None,
) -> dict[str, object]:
    """Contribution failures never rewrite a successfully captured candidate."""

    record = prepare_candidate(candidate_path, state_root=state_root, public_root=public_root,
                               kind=kind, public_relpath=public_relpath)
    result: dict[str, object] = {"blocked_capture": False, "pending": record.to_dict()}
    if record.status == STATUS_BLOCKED:
        return result
    if git_repo is None or github is None or config is None:
        if record.status not in {STATUS_PR_OPEN, "submitted", "merged", "closed"}:
            record.status = STATUS_AWAITING
            record.last_error = record.last_error or "submit transport not configured"
            record = save_pending(state_root, record)
        result["pending"] = record.to_dict()
        return result
    submitted = submit_pending(record, state_root=state_root, public_root=public_root,
                               git_repo=git_repo, github=github, config=config)
    result["pending"] = submitted.to_dict()
    return result


def prepare_public_copy_from_path(
    candidate_path: Path, public_root: Path, *, kind: str = "knowledge", public_relpath: str | None = None,
) -> PublicCopy:
    original = Path(candidate_path).read_bytes()
    copy = prepare_public_copy(original.decode("utf-8"), public_root=public_root, kind=kind,
                               public_relpath=public_relpath)
    if Path(candidate_path).read_bytes() != original:
        raise RuntimeError("candidate file was mutated while preparing a public copy")
    return copy
