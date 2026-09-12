"""Lightweight, account-level feedback on public experience documents.

Each stable public path has a GitHub issue. Reactions express an account's
feedback, not task counts or correctness. No candidate text, reason, runtime
context, corpus edit, or local feedback database is involved. GitHub has no
unique issue-creation key: simultaneous first use by different clients can
still race, while retries first discover an already-created issue/reaction.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Protocol
from urllib.parse import quote, unquote, urlsplit

from vaws_knowledge.contribution.documents import require_public_relpath, require_relative_path
from vaws_knowledge.contribution.errors import IdentityError
from vaws_knowledge.contribution.github import GitHubError, UrllibContributionGitHub
from vaws_knowledge.github_transport import github_token, repository_name

_MARKER_PREFIX = "<!-- vaws-experience-feedback: "
_TITLE_PREFIX = "Experience feedback: "
_SHARED_PREFIX = "/shared/"
_VERSION = r"v[0-9a-f]{12}"


class FeedbackGitHub(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...
    def delete(self, path: str) -> Any: ...


class UrllibFeedbackGitHub(UrllibContributionGitHub):
    def delete(self, path: str) -> Any:
        payload, _headers = self._request("DELETE", path)
        return payload


class _FeedbackError(Exception):
    def __init__(self, status: str, reason: str, *, retryable: bool = False):
        self.status = status
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


def experience_path(ref: str) -> str:
    """Resolve only an experience path or one of the package's shared URIs."""

    if not isinstance(ref, str) or not ref or ref != ref.strip():
        raise IdentityError("expected a public experience path or shared experience URI")
    relative = ref
    if "://" in ref:
        parsed = urlsplit(ref)
        if (parsed.scheme != "viking" or parsed.netloc != "resources"
                or parsed.query or parsed.fragment or not parsed.path.startswith(_SHARED_PREFIX)):
            raise IdentityError("feedback accepts only shared experience URIs")
        relative = unquote(parsed.path[len(_SHARED_PREFIX):], errors="strict")
        if not relative.startswith("experience/"):
            namespace = re.match(rf"(?:bootstrap|{_VERSION}|repairs/[0-9a-f]{{16}}/{_VERSION})/", relative)
            if namespace is None:
                raise IdentityError("unknown shared experience namespace")
            relative = relative[namespace.end():]
    return require_public_relpath(relative, "experience")


def _positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _pages(github: FeedbackGitHub, path: str):
    page = 1
    separator = "&" if "?" in path else "?"
    while True:
        items = github.get(f"{path}{separator}per_page=100&page={page}")
        if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
            raise _FeedbackError("error", "GitHub returned a malformed listing.", retryable=True)
        yield from items
        if len(items) < 100:
            return
        page += 1


def _issue(github: FeedbackGitHub, repository: str, relative: str, link: str) -> int:
    marker = f"{_MARKER_PREFIX}{relative} -->"
    full_title = _TITLE_PREFIX + relative
    title = full_title[:256]
    # Direct listing avoids the delay of GitHub's search index after a POST
    # whose response was lost. Oldest matching issue remains the stable target.
    for item in _pages(github, f"/repos/{repository}/issues?state=all&sort=created&direction=asc"):
        if "pull_request" in item:
            continue
        body = item.get("body") or ""
        if not isinstance(body, str):
            continue
        exact_marker = marker in body.splitlines()
        if exact_marker and _positive_id(item.get("number")):
            return item["number"]
    label = relative.replace("[", "\\[").replace("]", "\\]")
    created = github.post(f"/repos/{repository}/issues", {
        "title": title, "body": f"[{label}]({link})\n\n{marker}\n",
    })
    if not isinstance(created, Mapping) or not _positive_id(created.get("number")) or "pull_request" in created:
        raise _FeedbackError("error", "GitHub returned a malformed feedback issue.", retryable=True)
    return created["number"]


def _reactions(github: FeedbackGitHub, path: str) -> list[Mapping[str, Any]]:
    return list(_pages(github, path))


def _own(reaction: Mapping[str, Any], user_id: int) -> bool:
    user = reaction.get("user")
    return (isinstance(user, Mapping) and _positive_id(user.get("id"))
            and user["id"] == user_id and reaction.get("content") in {"+1", "-1"})


def experience_feedback(config: Any, ref: str, vote: str, *, github: FeedbackGitHub | None = None) -> dict[str, Any]:
    """Set this GitHub account's +1/-1 vote; return failures without raising."""

    settings = getattr(config, "publishing", {}) or {}
    if not isinstance(settings, Mapping) or not settings.get("enabled") or not settings.get("fork"):
        return {"status": "disabled", "reason": "Public sharing is not configured."}
    if not isinstance(vote, str) or vote not in {"+1", "-1"}:
        return {"status": "invalid_vote", "reason": "vote must be '+1' or '-1'.", "retryable": False}
    try:
        relative = experience_path(ref)
    except (IdentityError, ValueError, UnicodeError):
        return {"status": "invalid_ref", "reason": "Use a public experience path or shared experience URI.", "retryable": False}
    result: dict[str, Any] = {}
    try:
        shared = getattr(config, "shared_sync", {}) or {}
        repository = repository_name(settings.get("repository") or shared.get("repository") or "")
        base_ref = settings.get("base_ref") or settings.get("default_branch") or shared.get("base_ref") or shared.get("default_branch") or "main"
        require_relative_path(base_ref)
        prefix = settings.get("knowledge_prefix", "corpus")
        if prefix:
            require_relative_path(prefix)
        repo_path = "/".join(part for part in (prefix, relative) if part)
        github = github or UrllibFeedbackGitHub(github_token())
        try:
            document = github.get(f"/repos/{repository}/contents/{quote(repo_path, safe='/')}?ref={quote(base_ref, safe='')}")
        except GitHubError as exc:
            if exc.status == 404:
                raise _FeedbackError("not_found", "The experience is not published on the configured base branch.") from exc
            raise
        if not isinstance(document, Mapping) or document.get("type") != "file" or document.get("path") != repo_path:
            raise _FeedbackError("not_found", "The ref does not identify a published experience file.")
        user = github.get("/user")
        if not isinstance(user, Mapping) or not _positive_id(user.get("id")):
            raise _FeedbackError("error", "GitHub could not identify the current account.", retryable=True)
        user_id = user["id"]
        link = f"https://github.com/{repository}/blob/{quote(base_ref, safe='')}/{quote(repo_path, safe='/')}"
        number = _issue(github, repository, relative, link)
        result["issue_url"] = f"https://github.com/{repository}/issues/{number}"
        endpoint = f"/repos/{repository}/issues/{number}/reactions"
        reactions = _reactions(github, endpoint)
        mine = [item for item in reactions if _own(item, user_id)]
        if not any(item["content"] == vote for item in mine):
            # Add first: a failed request never removes the existing vote.
            # GitHub itself returns 200 for an already-existing same reaction.
            github.post(endpoint, {"content": vote})
        for item in mine:
            if item["content"] != vote:
                if not _positive_id(item.get("id")):
                    raise _FeedbackError("error", "GitHub returned a malformed reaction.", retryable=True)
                try:
                    github.delete(f"{endpoint}/{item['id']}")
                except GitHubError as exc:
                    if exc.status != 404:  # another client may have removed it
                        raise
        current = _reactions(github, endpoint)
        votes = {item["content"] for item in current if _own(item, user_id)}
        if votes != {vote}:
            raise _FeedbackError("error", "The vote changed concurrently; retry to confirm your choice.", retryable=True)
        counts = {sign: sum(item.get("content") == sign for item in current) for sign in ("+1", "-1")}
        return {"status": "ok", **result, "counts": counts, "vote": vote}
    except _FeedbackError as exc:
        return {"status": exc.status, **result, "reason": exc.reason, "retryable": exc.retryable}
    except GitHubError as exc:
        reason = ("GitHub authentication or permission is unavailable." if exc.status in {401, 403}
                  else "GitHub feedback is temporarily unavailable.")
        return {"status": "error", **result, "reason": reason, "retryable": True}
    except (IdentityError, ValueError, TypeError):
        return {"status": "error", **result, "reason": "Feedback repository configuration is invalid.", "retryable": False}
    except Exception:  # Feedback is optional; failed auth/transport must not interrupt the calling task.
        return {"status": "error", **result, "reason": "GitHub feedback transport is unavailable.", "retryable": True}
