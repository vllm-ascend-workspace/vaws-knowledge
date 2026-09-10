"""Conditional merge with head/base binding and trusted serialization.

GitHub's merge ``sha`` parameter only compare-and-swaps the PR head. Two
reviews that both passed against an old base must not both land: this module
re-reads the live default-branch SHA, holds a per-base lock, and refuses
when the base has moved. That is a content-update boundary, not a one-shot
check followed by an unconditional merge.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from vaws_knowledge.bot.publish_comment import GitHubError, _sha
from vaws_knowledge.contribution.errors import MergeBusy, TransportError
from vaws_knowledge.contribution.github import (
    GitHubTransport,
    default_branch_sha,
    live_pull,
    pull_base_sha,
    pull_head_sha,
    raise_transport,
    transport_message,
)
from vaws_knowledge.contribution.review import (
    ACTION_RE_REVIEW,
    ACTION_UNAVAILABLE,
    ReviewResult,
)

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows is listed unverified
    fcntl = None  # type: ignore[assignment]


@dataclass
class MergeOutcome:
    merged: bool
    action: str
    reason: str
    sha: str | None = None
    pr_number: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "merged": self.merged,
            "action": self.action,
            "reason": self.reason,
            "sha": self.sha,
            "pr_number": self.pr_number,
        }


class MemorySerializer:
    """In-process lock keyed by (repository, base_sha)."""

    def __init__(self) -> None:
        self._held: set[tuple[str, str]] = set()
        self._mutex = threading.Lock()

    @contextlib.contextmanager
    def hold(self, repository: str, base_sha: str) -> Iterator[None]:
        key = (repository, base_sha)
        with self._mutex:
            if key in self._held:
                raise MergeBusy(f"merge already in progress for {repository} at {base_sha}")
            self._held.add(key)
        try:
            yield
        finally:
            with self._mutex:
                self._held.discard(key)


class FileSerializer:
    """Advisory file lock under a state root. Not a generic job queue."""

    def __init__(self, state_root: Path) -> None:
        self.root = Path(state_root) / "contribution" / "merge-locks"

    @contextlib.contextmanager
    def hold(self, repository: str, base_sha: str) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        safe = repository.replace("/", "_") + "-" + base_sha
        path = self.root / safe
        handle = path.open("a+")
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise MergeBusy(f"merge already in progress for {repository} at {base_sha}") from exc
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _refused(action: str, reason: str, pr_number: int | None = None) -> MergeOutcome:
    return MergeOutcome(merged=False, action=action, reason=reason, pr_number=pr_number)


def merge_reviewed(
    review: ReviewResult,
    *,
    github: GitHubTransport,
    repository: str,
    pr_number: int,
    serializer: MemorySerializer | FileSerializer | None = None,
    default_branch: str | None = None,
) -> MergeOutcome:
    """Merge only if the live PR still matches the bound review."""

    if review.status != "success":
        return _refused(
            ACTION_UNAVAILABLE if review.status == "unavailable" else review.action,
            review.reason or "review is not a successful semantic decision",
            pr_number,
        )
    if not review.permits_publish:
        return _refused(review.action, review.reason or "review does not permit publish", pr_number)
    lock = serializer or MemorySerializer()
    try:
        with lock.hold(repository, review.base_sha):
            return _merge_locked(
                review,
                github=github,
                repository=repository,
                pr_number=pr_number,
                default_branch=default_branch,
            )
    except MergeBusy as exc:
        return _refused(ACTION_RE_REVIEW, str(exc), pr_number)
    except TransportError as exc:
        action = "error"
        if exc.status in (401, 403):
            action = "error"
        return _refused(action, str(exc), pr_number)


def _merge_locked(
    review: ReviewResult,
    *,
    github: GitHubTransport,
    repository: str,
    pr_number: int,
    default_branch: str | None,
) -> MergeOutcome:
    pull = live_pull(github, repository, pr_number)
    if pull.get("state") != "open":
        return _refused("error", "pull request is not open", pr_number)
    live_head = pull_head_sha(pull)
    live_base = pull_base_sha(pull)
    if live_head != review.candidate_head:
        return _refused(ACTION_RE_REVIEW, "stale head", pr_number)
    if live_base != review.base_sha:
        return _refused(ACTION_RE_REVIEW, "stale base", pr_number)
    base_ref = default_branch
    base_obj = pull.get("base")
    if not base_ref and isinstance(base_obj, dict):
        ref = base_obj.get("ref")
        if isinstance(ref, str) and ref:
            base_ref = ref
    if not base_ref:
        return _refused("error", "pull request is missing a base branch", pr_number)
    current_default = default_branch_sha(github, repository, base_ref)
    if current_default != review.base_sha:
        return _refused(ACTION_RE_REVIEW, "stale base", pr_number)
    try:
        result = github.put(
            f"/repos/{repository}/pulls/{pr_number}/merge",
            {
                "sha": review.candidate_head,
                "merge_method": "merge",
                "commit_title": review.title or pull.get("title") or "knowledge contribution",
            },
        )
    except GitHubError as exc:
        if exc.status in (405, 409):
            return _refused(ACTION_RE_REVIEW, transport_message(exc), pr_number)
        raise_transport(exc)
        raise
    if not isinstance(result, dict) or not result.get("merged"):
        return _refused("error", "GitHub merge did not confirm merged=true", pr_number)
    after = default_branch_sha(github, repository, base_ref)
    if after == review.base_sha:
        return _refused("error", "merge did not advance the default branch", pr_number)
    merged_sha = _sha(result.get("sha")) or after
    return MergeOutcome(
        merged=True,
        action="merged",
        reason="merged with matching head and base",
        sha=merged_sha,
        pr_number=pr_number,
    )


def close_duplicate(
    *,
    github: GitHubTransport,
    repository: str,
    pr_number: int,
    review: ReviewResult,
) -> dict[str, object]:
    """Close a duplicate PR. Does not merge."""

    if review.decision != "duplicate":
        return {"closed": False, "reason": "review is not a duplicate"}
    try:
        github.post(
            f"/repos/{repository}/issues/{pr_number}/comments",
            {"body": f"Duplicate of related published content. {review.reason}"},
        )
        github.patch(f"/repos/{repository}/pulls/{pr_number}", {"state": "closed"})
    except GitHubError as exc:
        raise_transport(exc)
        raise
    return {"closed": True, "reason": review.reason, "pr_number": pr_number}
