"""Independent feedback events on public experience documents.

Each stable public path has one GitHub issue. Each actual use can add a +1 or
-1 comment; counts express feedback events, not correctness. Comments contain
only the vote and an opaque request ID, with no reason or runtime context.
Retries with that returned ID are deduplicated by GitHub author and request ID.
There is no local database, worker, or corpus edit. Calls without an ID are new
events, including after a lost whole-tool response. GitHub has no atomic issue
or comment creation key; concurrent copies of an event count only once.
"""

from __future__ import annotations

import re
import uuid
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
_VOTE_BODY = re.compile(r"([+-]1)\n\n<!-- vaws-experience-vote: ([0-9a-f]{32}) -->\n?")


class FeedbackGitHub(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...


class _FeedbackError(Exception):
    def __init__(self, status: str, reason: str, *, retryable: bool = False):
        self.status = status
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


def experience_path(ref: str) -> str:
    """Resolve only an experience path or one of the package's shared URIs."""

    if not isinstance(ref, str) or not ref or ref != ref.strip() or any(ord(char) < 32 for char in ref):
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


def _events(github: FeedbackGitHub, path: str) -> dict[tuple[int, str], tuple[str, int]]:
    """The earliest complete comment for an author's request ID is its event."""

    events: dict[tuple[int, str], tuple[str, int]] = {}
    comments = list(_pages(github, path))
    for comment in sorted(comments, key=lambda item: item.get("id") if _positive_id(item.get("id")) else 0):
        user = comment.get("user")
        body = comment.get("body")
        if (not isinstance(user, Mapping) or not _positive_id(user.get("id"))
                or not _positive_id(comment.get("id")) or not isinstance(body, str)):
            continue
        match = _VOTE_BODY.fullmatch(body)
        if match is None:
            continue
        vote, request_id = match.groups()
        events.setdefault((user["id"], request_id), (vote, comment["id"]))
    return events


def experience_feedback(
    config: Any, ref: str, vote: str, *, github: FeedbackGitHub | None = None, request_id: str | None = None,
) -> dict[str, Any]:
    """Record one use; reuse an error's request ID only when retrying that use."""

    if request_id is None:
        request_id = uuid.uuid4().hex
    elif not isinstance(request_id, str) or re.fullmatch(r"[0-9a-fA-F]{32}", request_id) is None:
        return {"status": "invalid_request_id", "reason": "request_id must be an opaque 32-character hexadecimal UUID.", "retryable": False}
    request_id = request_id.lower()
    result: dict[str, Any] = {"request_id": request_id}
    settings = getattr(config, "publishing", {}) or {}
    if not isinstance(settings, Mapping) or not settings.get("enabled") or not settings.get("fork"):
        return {"status": "disabled", **result, "reason": "Public sharing is not configured."}
    if not isinstance(vote, str) or vote not in {"+1", "-1"}:
        return {"status": "invalid_vote", **result, "reason": "vote must be '+1' or '-1'.", "retryable": False}
    try:
        relative = experience_path(ref)
    except (IdentityError, ValueError, UnicodeError):
        return {"status": "invalid_ref", **result, "reason": "Use a public experience path or shared experience URI.", "retryable": False}
    try:
        shared = getattr(config, "shared_sync", {}) or {}
        repository = repository_name(settings.get("repository") or shared.get("repository") or "")
        base_ref = settings.get("base_ref") or settings.get("default_branch") or shared.get("base_ref") or shared.get("default_branch") or "main"
        require_relative_path(base_ref)
        prefix = settings.get("knowledge_prefix", "corpus")
        if prefix:
            require_relative_path(prefix)
        repo_path = "/".join(part for part in (prefix, relative) if part)
    except (IdentityError, ValueError, TypeError, AttributeError):
        return {"status": "error", **result, "reason": "Feedback repository configuration is invalid.", "retryable": False}
    try:
        github = github or UrllibContributionGitHub(github_token())
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
        endpoint = f"/repos/{repository}/issues/{number}/comments"
        events = _events(github, endpoint)
        key = (user_id, request_id)
        if key not in events:
            post_error: Exception | None = None
            try:
                github.post(endpoint, {"body": f"{vote}\n\n<!-- vaws-experience-vote: {request_id} -->\n"})
            except Exception as exc:
                # An uncertain POST is never sent a second time in this call.
                # Read back the author's event before deciding it failed.
                post_error = exc
            events = _events(github, endpoint)
            if key not in events:
                if post_error is not None:
                    raise post_error
                raise _FeedbackError("error", "The feedback event could not be confirmed; retry with this request_id.", retryable=True)
        recorded_vote, comment_id = events[key]
        if recorded_vote != vote:
            raise _FeedbackError("conflict", "This request_id already records a different vote.")
        counts = {sign: sum(event[0] == sign for event in events.values()) for sign in ("+1", "-1")}
        return {"status": "ok", "issue_url": result["issue_url"],
                "feedback_url": f"{result['issue_url']}#issuecomment-{comment_id}", "counts": counts, "vote": vote}
    except _FeedbackError as exc:
        return {"status": exc.status, **result, "reason": exc.reason, "retryable": exc.retryable}
    except GitHubError as exc:
        reason = ("GitHub authentication or permission is unavailable." if exc.status in {401, 403}
                  else "GitHub feedback is temporarily unavailable.")
        return {"status": "error", **result, "reason": reason, "retryable": True}
    except Exception:  # Feedback is optional; failed auth/transport must not interrupt the calling task.
        return {"status": "error", **result, "reason": "GitHub feedback transport is unavailable.", "retryable": True}
