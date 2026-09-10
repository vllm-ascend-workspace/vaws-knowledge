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
from vaws_knowledge.contribution.recall import Recall
from vaws_knowledge.contribution.review import ReviewResult, review_candidate
from vaws_knowledge.sync.collect import decode_git_blob, git_blob_sha1

TRUSTED_WORKFLOW_NAME = "Trusted knowledge review"
TEMPLATE_RELATIVE = "examples/corpus-contribution/trusted-review.yml"
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


def is_secret_bearing_checkout(ref: str | None, *, default_branch: str, event_head: str) -> bool:
    """True when a secret job would check out PR/fork code."""

    if not ref:
        return True
    if ref in {event_head, "refs/pull/head", "${{ github.event.pull_request.head.sha }}"}:
        return True
    if ref == default_branch or ref == "${{ github.event.repository.default_branch }}":
        return False
    return False


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


def run_trusted_ci(
    event: Mapping[str, Any],
    *,
    github: GitHubTransport,
    repository: str,
    stash: Path,
    recall: Recall,
    classifier: Any,
    checkout_ref: str | None,
    default_branch: str,
    execute_paths: list[str] | None = None,
    install_fork: bool = False,
) -> dict[str, Any]:
    """Review PR Markdown as data using trusted package code."""

    if install_fork:
        return {
            "status": "error",
            "reason": "secret-bearing path must not install fork code",
            "permits_publish": False,
        }
    if execute_paths:
        return {
            "status": "error",
            "reason": "secret-bearing path must not execute pull-request files",
            "permits_publish": False,
            "refused_execute": list(execute_paths),
        }
    try:
        number, head, base = _event_pull(event, repository, github)
    except TransportError as exc:
        return {
            "status": "unavailable" if (exc.status in (0, 401, 403) or "stale" in str(exc)) else "error",
            "reason": str(exc),
            "permits_publish": False,
        }
    if is_secret_bearing_checkout(checkout_ref, default_branch=default_branch, event_head=head):
        return {
            "status": "error",
            "reason": "secret-bearing path must not checkout pull-request head",
            "permits_publish": False,
        }
    try:
        fetched = fetch_pr_markdown(github, repository, head, stash)
    except TransportError as exc:
        return {
            "status": "unavailable" if exc.status in (0, 401, 403) else "error",
            "reason": str(exc),
            "permits_publish": False,
        }
    if fetched.failed:
        return {
            "status": "unavailable",
            "reason": "selected markdown blob fetch failed",
            "permits_publish": False,
            "fetch": {"failed": fetched.failed, "omitted": fetched.omitted},
        }
    markdown_files = [path for path in fetched.files if path.suffix == ".md"]
    if not markdown_files:
        return {
            "status": "unavailable",
            "reason": "no eligible markdown data in the pull request",
            "permits_publish": False,
            "fetch": {"omitted": fetched.omitted},
        }
    # One contribution PR is one Markdown document in this delivery.
    text = markdown_files[0].read_text(encoding="utf-8")
    review = review_candidate(
        text,
        candidate_head=head,
        base_sha=base,
        recall=recall,
        classifier=classifier,
        path=str(markdown_files[0].name),
    )
    payload = review.as_dict()
    payload["pr_number"] = number
    payload["repository"] = repository
    payload["fetch"] = {
        "files": [str(path) for path in fetched.files],
        "omitted": fetched.omitted,
        "failed": fetched.failed,
    }
    payload["trusted"] = {
        "workflow": TRUSTED_WORKFLOW_NAME,
        "template": TEMPLATE_RELATIVE,
        "checkout_ref": checkout_ref,
        "installed_fork": False,
        "executed_pr_files": False,
    }
    return payload


def maybe_merge_from_ci(
    review_payload: Mapping[str, Any],
    *,
    github: GitHubTransport,
    serializer: Any | None = None,
) -> MergeOutcome:
    review = ReviewResult.from_dict(dict(review_payload))
    repository = str(review_payload.get("repository") or "")
    pr_number = review_payload.get("pr_number")
    if not repository or not isinstance(pr_number, int):
        return MergeOutcome(merged=False, action="error", reason="review payload missing repository or pr")
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
