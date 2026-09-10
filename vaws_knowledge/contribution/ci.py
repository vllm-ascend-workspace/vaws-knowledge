"""Trusted CI runner: PR files are data, secrets stay on trusted package code.

The secret-bearing path must not check out, execute, or install fork code.
Permission and provider errors are explicit. External unavailability is not
a review pass. The workflow file shipped under examples is a template and is
not enabled in this repository.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from vaws_knowledge.bot.publish_comment import GitHubError, _sha
from vaws_knowledge.contribution.documents import require_git_sha
from vaws_knowledge.contribution.errors import TransportError
from vaws_knowledge.contribution.github import (
    GitHubTransport,
    live_pull,
    pull_base_sha,
    pull_head_sha,
    raise_transport,
)
from vaws_knowledge.contribution.merge import MergeOutcome, merge_reviewed
from vaws_knowledge.contribution.recall import Recall, production_recall
from vaws_knowledge.contribution.review import (
    ACTION_HOLD,
    ACTION_UNAVAILABLE,
    DECISION_CONFLICT,
    ReviewResult,
    review_candidate,
)
from vaws_knowledge.sync.collect import decode_git_blob, git_blob_sha1

TRUSTED_WORKFLOW_NAME = "Trusted knowledge review"
TEMPLATE_RELATIVE = "examples/corpus-contribution/trusted-review.yml.tmpl"
KNOWLEDGE_ROOTS = ("corpus", "shared")
NON_KNOWLEDGE_MARKDOWN = {
    "readme.md",
    "contributing.md",
    "changelog.md",
    "license.md",
    "code_of_conduct.md",
}
ORDINARY_BLOB_MODE = "100644"
MAX_FILE_BYTES = 1_048_576
FORBIDDEN_PREFIXES = (".github/workflows/",)
FORBIDDEN_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
}


@dataclass
class FetchedData:
    files: list[Path] = field(default_factory=list)
    omitted: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)


@dataclass
class FileChange:
    path: str
    status: str
    head_sha: str | None = None
    base_sha: str | None = None
    previous_path: str | None = None


@dataclass
class ChangeSet:
    changes: list[FileChange] = field(default_factory=list)
    truncated: bool = False
    incomplete: bool = False
    reason: str = ""


def is_secret_bearing_checkout(ref: str | None, *, default_branch: str, event_head: str) -> bool:
    """True when a secret job would check out PR/fork code."""

    if not ref:
        return True
    if ref in {event_head, "refs/pull/head", "${{ github.event.pull_request.head.sha }}"}:
        return True
    if ref == default_branch or ref == "${{ github.event.repository.default_branch }}":
        return False
    return False


def permitted_knowledge_path(path: str) -> bool:
    """True when this path may land in an automatic knowledge merge."""

    posix = path.replace("\\", "/").lstrip("/")
    if allowed_data_path(posix) is not None:
        return False
    name = posix.rsplit("/", 1)[-1].lower()
    if name in NON_KNOWLEDGE_MARKDOWN:
        return False
    root = posix.split("/", 1)[0]
    return root in KNOWLEDGE_ROOTS and posix.endswith(".md")


def allowed_data_path(path: str) -> str | None:
    """Return a rejection reason, or None if the path may be read as Markdown data."""

    posix = path.replace("\\", "/")
    if not posix or posix.startswith("/") or ".." in posix.split("/"):
        return "path_traversal"
    if posix.startswith(FORBIDDEN_PREFIXES) or posix in FORBIDDEN_NAMES:
        return "secret_bearing_or_installable"
    name = posix.rsplit("/", 1)[-1]
    if name.startswith("."):
        return "hidden"
    if not posix.endswith(".md"):
        return "not_markdown_data"
    return None


def fetch_pr_markdown(
    api: GitHubTransport,
    repository: str,
    head_sha: str,
    stash: Path,
) -> FetchedData:
    """Fetch ordinary Markdown blobs at an immutable head. Never executes them."""

    head = require_git_sha(head_sha)
    stash = Path(stash)
    stash.mkdir(parents=True, exist_ok=True)
    resolved = stash.resolve()
    for item in __import__("sys").path:
        if not item:
            continue
        try:
            if Path(item).resolve() == resolved or str(Path(item).resolve()).startswith(str(resolved) + "/"):
                raise TransportError("refusing to proceed: fetched PR bytes are on sys.path")
        except OSError:
            continue
    result = FetchedData()
    try:
        payload = api.get(f"/repos/{repository}/git/trees/{head}?recursive=1")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(payload, Mapping) or not isinstance(payload.get("tree"), list):
        raise TransportError("malformed git tree")
    if payload.get("truncated"):
        result.omitted.append({"path": "", "reason": "truncated_tree"})
    for entry in payload["tree"]:
        if not isinstance(entry, Mapping):
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        reason = allowed_data_path(path)
        if reason is not None:
            if path.endswith((".py", ".sh", ".yml", ".yaml")) or path.startswith(".github/"):
                result.omitted.append({"path": path, "reason": reason})
            continue
        if entry.get("type") != "blob" or entry.get("mode") != ORDINARY_BLOB_MODE:
            result.omitted.append({"path": path, "reason": "not_ordinary_blob"})
            continue
        blob_sha = _sha(entry.get("sha"))
        if blob_sha is None:
            result.failed.append({"path": path, "reason": "missing_blob_sha"})
            continue
        try:
            blob = api.get(f"/repos/{repository}/git/blobs/{blob_sha}")
            data = decode_git_blob(blob, blob_sha, max_bytes=MAX_FILE_BYTES)
        except (GitHubError, Exception) as exc:  # noqa: BLE001
            result.failed.append({"path": path, "reason": str(exc) or "blob_fetch_failed"})
            continue
        dest = stash.joinpath(*path.split("/"))
        dest.resolve().relative_to(resolved)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        result.files.append(dest)
    return result


def _tree_index(payload: Mapping[str, Any]) -> tuple[dict[str, dict[str, str]], bool]:
    entries = payload.get("tree")
    if not isinstance(entries, list):
        raise TransportError("malformed git tree")
    index: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") != "blob":
            continue
        path = entry.get("path")
        sha = _sha(entry.get("sha"))
        mode = str(entry.get("mode") or "")
        if not isinstance(path, str) or sha is None:
            continue
        index[path.replace("\\", "/")] = {"sha": sha, "mode": mode}
    return index, bool(payload.get("truncated"))


def load_tree_index(api: GitHubTransport, repository: str, sha: str) -> tuple[dict[str, dict[str, str]], bool]:
    try:
        payload = api.get(f"/repos/{repository}/git/trees/{require_git_sha(sha)}?recursive=1")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(payload, Mapping):
        raise TransportError("malformed git tree")
    return _tree_index(payload)


def diff_trees(
    base_index: Mapping[str, Mapping[str, str]],
    head_index: Mapping[str, Mapping[str, str]],
) -> list[FileChange]:
    changes: list[FileChange] = []
    for path in sorted(set(base_index) | set(head_index)):
        before = base_index.get(path)
        after = head_index.get(path)
        base_sha = before.get("sha") if before else None
        head_sha = after.get("sha") if after else None
        if before is None and after is not None:
            changes.append(FileChange(path=path, status="added", head_sha=head_sha))
        elif after is None and before is not None:
            changes.append(FileChange(path=path, status="removed", base_sha=base_sha))
        elif base_sha != head_sha:
            changes.append(FileChange(path=path, status="modified", head_sha=head_sha, base_sha=base_sha))
    added = {item.path: item for item in changes if item.status == "added"}
    removed = {item.path: item for item in changes if item.status == "removed"}
    consumed: set[str] = set()
    for new_path, added_item in added.items():
        for old_path, removed_item in removed.items():
            if old_path in consumed:
                continue
            if added_item.head_sha and added_item.head_sha == removed_item.base_sha:
                added_item.status = "renamed"
                added_item.previous_path = old_path
                added_item.base_sha = removed_item.base_sha
                consumed.add(old_path)
                break
    return [item for item in changes if not (item.status == "removed" and item.path in consumed)]


def fetch_blob_bytes(api: GitHubTransport, repository: str, blob_sha: str) -> bytes:
    try:
        blob = api.get(f"/repos/{repository}/git/blobs/{blob_sha}")
        return decode_git_blob(blob, blob_sha, max_bytes=MAX_FILE_BYTES)
    except GitHubError as exc:
        raise_transport(exc)
        raise


def _event_pull(event: Mapping[str, Any], repository: str, api: GitHubTransport) -> tuple[int, str, str]:
    pull = event.get("pull_request")
    if not isinstance(pull, Mapping):
        raise TransportError("missing pull_request event payload")
    number = pull.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise TransportError("malformed pull request number")
    event_head = pull_head_sha(pull)
    event_base = pull_base_sha(pull)
    live = live_pull(api, repository, number)
    if pull_head_sha(live) != event_head:
        raise TransportError("stale head")
    if pull_base_sha(live) != event_base:
        raise TransportError("stale base")
    return number, event_head, event_base


def _closed(
    *,
    status: str,
    reason: str,
    permits_publish: bool = False,
    action: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "permits_publish": False if not permits_publish else permits_publish,
        "action": action or (ACTION_UNAVAILABLE if status == "unavailable" else "error"),
        "fetch": {"files": [], "omitted": [], "failed": []},
        "reviewed_titles": [],
        "unsupported_changes": [],
        "changed_paths": [],
    }
    payload.update(extra)
    return payload


def _decision_rank(decision: str | None) -> int:
    order = {
        "new": 10,
        "condition_difference": 20,
        "supplement": 50,
        "duplicate": 60,
        "insufficient_evidence": 70,
        "conflict": 80,
        None: 95,
    }
    return order.get(decision, 90)


def run_trusted_ci(
    event: Mapping[str, Any],
    *,
    github: GitHubTransport,
    repository: str,
    stash: Path,
    classifier: Any,
    checkout_ref: str | None,
    default_branch: str,
    recall: Recall | None = None,
    execute_paths: list[str] | None = None,
    install_fork: bool = False,
    environ: Mapping[str, str] | None = None,
    openviking_url: str | None = None,
    openviking_client: Any | None = None,
) -> dict[str, Any]:
    """Review the immutable base→head change set using trusted package code."""

    if install_fork:
        return _closed(status="error", reason="secret-bearing path must not install fork code")
    if execute_paths:
        return _closed(
            status="error",
            reason="secret-bearing path must not execute pull-request files",
            refused_execute=list(execute_paths),
        )
    try:
        number, head, base = _event_pull(event, repository, github)
    except TransportError as exc:
        status = "unavailable" if (exc.status in (0, 401, 403) or "stale" in str(exc)) else "error"
        return _closed(status=status, reason=str(exc))
    if is_secret_bearing_checkout(checkout_ref, default_branch=default_branch, event_head=head):
        return _closed(status="error", reason="secret-bearing path must not checkout pull-request head")
    bound_recall = recall
    if bound_recall is None:
        bound_recall = production_recall(
            base_sha=base,
            environ=environ,
            url=openviking_url,
            client=openviking_client,
        )
    expected_corpus = getattr(bound_recall, "corpus_git_sha", None)
    if expected_corpus is not None and expected_corpus != base:
        return _closed(
            status="unavailable",
            reason="recall corpus SHA does not match review base",
            candidate_head=head,
            base_sha=base,
            pr_number=number,
            repository=repository,
        )
    try:
        base_index, base_truncated = load_tree_index(github, repository, base)
        head_index, head_truncated = load_tree_index(github, repository, head)
    except TransportError as exc:
        status = "unavailable" if exc.status in (0, 401, 403) else "error"
        return _closed(status=status, reason=str(exc), candidate_head=head, base_sha=base, pr_number=number, repository=repository)
    if base_truncated or head_truncated:
        return _closed(
            status="unavailable",
            reason="truncated change list; missing diff evidence cannot permit publish",
            candidate_head=head,
            base_sha=base,
            pr_number=number,
            repository=repository,
            truncated=True,
        )
    changes = diff_trees(base_index, head_index)
    changed_paths = [item.path for item in changes]
    if item_previous := [item.previous_path for item in changes if item.previous_path]:
        changed_paths.extend(item_previous)
    unsupported = [item.path for item in changes if not permitted_knowledge_path(item.path)]
    knowledge = [item for item in changes if permitted_knowledge_path(item.path)]
    omitted = [{"path": path, "reason": allowed_data_path(path) or "outside_corpus_scope"} for path in unsupported]
    stash_root = Path(stash)
    stash_root.mkdir(parents=True, exist_ok=True)
    reviews: list[ReviewResult] = []
    reviewed_rows: list[dict[str, Any]] = []
    fetched_files: list[str] = []
    failed: list[dict[str, str]] = []
    for change in knowledge:
        if change.status == "removed":
            row = ReviewResult(
                status="success",
                decision=None,
                action=ACTION_HOLD,
                reason=f"deletion of {change.path} is not auto-merged",
                candidate_head=head,
                base_sha=base,
                permits_publish=False,
                title=change.path,
            )
            reviews.append(row)
            reviewed_rows.append({"path": change.path, "status": "removed", "title": change.path, "decision": None, "permits_publish": False})
            continue
        blob_sha = change.head_sha
        if blob_sha is None:
            failed.append({"path": change.path, "reason": "missing_blob_sha"})
            continue
        try:
            data = fetch_blob_bytes(github, repository, blob_sha)
        except (TransportError, Exception) as exc:  # noqa: BLE001
            failed.append({"path": change.path, "reason": str(exc) or "blob_fetch_failed"})
            continue
        dest = stash_root.joinpath(*change.path.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        fetched_files.append(str(dest))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            failed.append({"path": change.path, "reason": "not_utf8"})
            continue
        result = review_candidate(
            text,
            candidate_head=head,
            base_sha=base,
            recall=bound_recall,
            classifier=classifier,
            path=change.path,
        )
        reviews.append(result)
        reviewed_rows.append(
            {
                "path": change.path,
                "status": change.status,
                "previous_path": change.previous_path,
                "title": result.title,
                "decision": result.decision,
                "permits_publish": result.permits_publish,
                "action": result.action,
            }
        )
    if failed:
        return _closed(
            status="unavailable",
            reason="selected markdown blob fetch failed",
            candidate_head=head,
            base_sha=base,
            pr_number=number,
            repository=repository,
            fetch={"failed": failed, "omitted": omitted, "files": fetched_files},
            changed_paths=changed_paths,
            unsupported_changes=unsupported,
            reviewed=reviewed_rows,
        )
    if not knowledge:
        return _closed(
            status="unavailable",
            reason="no eligible markdown knowledge changes in the pull request",
            candidate_head=head,
            base_sha=base,
            pr_number=number,
            repository=repository,
            fetch={"omitted": omitted, "files": fetched_files},
            changed_paths=changed_paths,
            unsupported_changes=unsupported,
            reviewed=reviewed_rows,
        )
    worst = max(reviews, key=lambda item: (_decision_rank(item.decision), 0 if item.status == "success" else 1))
    payload = worst.as_dict()
    payload["pr_number"] = number
    payload["repository"] = repository
    payload["candidate_head"] = head
    payload["base_sha"] = base
    payload["reviewed"] = reviewed_rows
    payload["reviewed_titles"] = [row.get("title") for row in reviewed_rows]
    payload["changed_paths"] = changed_paths
    payload["unsupported_changes"] = unsupported
    payload["fetch"] = {"files": fetched_files, "omitted": omitted, "failed": failed}
    payload["trusted"] = {
        "workflow": TRUSTED_WORKFLOW_NAME,
        "template": TEMPLATE_RELATIVE,
        "checkout_ref": checkout_ref,
        "installed_fork": False,
        "executed_pr_files": False,
    }
    all_ok = all(item.status == "success" for item in reviews)
    all_permit = all(item.permits_publish for item in reviews)
    if not all_ok:
        payload["status"] = next((item.status for item in reviews if item.status != "success"), "error")
        payload["permits_publish"] = False
        payload["action"] = ACTION_UNAVAILABLE if payload["status"] == "unavailable" else payload.get("action")
        payload["reason"] = next((item.reason for item in reviews if item.status != "success"), payload.get("reason"))
    elif unsupported:
        payload["permits_publish"] = False
        payload["action"] = "refuse_unsupported"
        payload["reason"] = "unsupported changes are outside the permitted corpus scope: " + ", ".join(unsupported)
        payload["status"] = "success"
    elif not all_permit:
        payload["permits_publish"] = False
        if any(item.decision == DECISION_CONFLICT for item in reviews):
            payload["decision"] = DECISION_CONFLICT
            payload["action"] = next(item.action for item in reviews if item.decision == DECISION_CONFLICT)
            payload["reason"] = next(item.reason for item in reviews if item.decision == DECISION_CONFLICT)
            payload["title"] = next(item.title for item in reviews if item.decision == DECISION_CONFLICT)
    else:
        payload["permits_publish"] = True
    return payload


def maybe_merge_from_ci(
    review_payload: Mapping[str, Any],
    *,
    github: GitHubTransport,
    serializer: Any | None = None,
) -> MergeOutcome:
    pr_number = review_payload.get("pr_number")
    repository = str(review_payload.get("repository") or "")
    if not review_payload.get("permits_publish"):
        return MergeOutcome(
            merged=False,
            action=str(review_payload.get("action") or "refused"),
            reason=str(review_payload.get("reason") or "review does not permit publish"),
            pr_number=pr_number if isinstance(pr_number, int) else None,
        )
    if review_payload.get("unsupported_changes"):
        return MergeOutcome(
            merged=False,
            action="refuse_unsupported",
            reason="unsupported changes cannot be merged",
            pr_number=pr_number if isinstance(pr_number, int) else None,
        )
    if not repository or not isinstance(pr_number, int):
        return MergeOutcome(merged=False, action="error", reason="review payload missing repository or pr")
    try:
        review = ReviewResult.from_dict(dict(review_payload))
    except Exception as exc:  # noqa: BLE001
        return MergeOutcome(merged=False, action="error", reason=f"malformed review binding: {exc}")
    return merge_reviewed(
        review,
        github=github,
        repository=repository,
        pr_number=pr_number,
        serializer=serializer,
    )


def encode_blob(data: bytes) -> dict[str, Any]:
    sha = git_blob_sha1(data)
    return {
        "sha": sha,
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "size": len(data),
    }
