"""Injectable GitHub transport for contribution PRs and conditional merge.

Reuses the published error types, SHA checks, and REST conventions from
``bot.publish_comment``. Adds PUT (merge) without modifying that module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Mapping, Optional, Protocol

from vaws_knowledge.bot.publish_comment import (
    API_ROOT,
    API_VERSION,
    GitHubError,
    _full_name,
    _next_link,
    _sha,
)
from vaws_knowledge.contribution.documents import require_git_sha
from vaws_knowledge.contribution.errors import TransportError

ORDINARY_BLOB_MODE = "100644"
MAX_BLOB_BYTES = 1_048_576


class GitHubTransport(Protocol):
    def get(self, path: str) -> Any: ...

    def paginate(self, path: str) -> list[Any]: ...

    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...

    def put(self, path: str, body: Mapping[str, Any]) -> Any: ...

    def patch(self, path: str, body: Mapping[str, Any]) -> Any: ...


def transport_message(exc: GitHubError) -> str:
    if exc.status in (401, 403):
        return f"GitHub permission error ({exc.status}) for {exc.path}"
    if exc.status == 0:
        return f"GitHub network error for {exc.path}: {exc}"
    return f"GitHub API {exc.status} for {exc.path}"


def raise_transport(exc: GitHubError) -> None:
    raise TransportError(transport_message(exc), status=exc.status) from exc


class UrllibContributionGitHub:
    """Authenticated GitHub REST transport. Tests can inject the same interface."""

    def __init__(self, token: str) -> None:
        if not token:
            raise TransportError("missing GITHUB_TOKEN", status=401)
        self._token = token

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else API_ROOT + path
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise TransportError("GitHub API requests must stay on api.github.com")
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "vaws-knowledge-contribution",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                header_map = {key: value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise GitHubError(exc.code, path, detail) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(0, path, str(exc)) from exc
        payload: Any = None
        if raw:
            payload = json.loads(raw.decode("utf-8"))
        return payload, header_map

    def get(self, path: str) -> Any:
        payload, _headers = self._request("GET", path)
        return payload

    def paginate(self, path: str) -> list[Any]:
        items: list[Any] = []
        current: Optional[str] = path
        while current:
            payload, headers = self._request("GET", current)
            if not isinstance(payload, list):
                raise TransportError("malformed GitHub listing")
            items.extend(payload)
            current = _next_link(headers.get("Link") or headers.get("link"))
        return items

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        payload, _headers = self._request("POST", path, body)
        return payload

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        payload, _headers = self._request("PUT", path, body)
        return payload

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        payload, _headers = self._request("PATCH", path, body)
        return payload


def pull_head_ref(fork: str, upstream: str, branch: str) -> str:
    if fork == upstream:
        return branch
    owner = fork.split("/", 1)[0]
    return f"{owner}:{branch}"


def default_branch_sha(api: GitHubTransport, repository: str, branch: str) -> str:
    try:
        payload = api.get(f"/repos/{repository}/git/ref/heads/{branch}")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(payload, Mapping):
        raise TransportError("malformed git ref")
    obj = payload.get("object")
    sha = _sha(obj.get("sha") if isinstance(obj, Mapping) else None)
    if sha is None:
        raise TransportError("malformed git ref sha")
    return sha


def live_pull(api: GitHubTransport, repository: str, number: int) -> dict[str, Any]:
    try:
        payload = api.get(f"/repos/{repository}/pulls/{number}")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(payload, Mapping):
        raise TransportError("malformed pull request")
    return dict(payload)


def pull_head_sha(pull: Mapping[str, Any]) -> str:
    head = pull.get("head")
    sha = _sha(head.get("sha") if isinstance(head, Mapping) else None)
    if sha is None:
        raise TransportError("pull request is missing a git head")
    return sha


def pull_base_sha(pull: Mapping[str, Any]) -> str:
    base = pull.get("base")
    sha = _sha(base.get("sha") if isinstance(base, Mapping) else None)
    if sha is None:
        raise TransportError("pull request is missing a git base")
    return sha


def find_open_pull(
    api: GitHubTransport,
    *,
    upstream: str,
    fork: str,
    branch: str,
) -> dict[str, Any] | None:
    head = pull_head_ref(fork, upstream, branch)
    try:
        payload = api.get(f"/repos/{upstream}/pulls?head={head}&state=open")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(payload, list):
        raise TransportError("malformed pull listing")
    for item in payload:
        if isinstance(item, Mapping) and isinstance(item.get("number"), int):
            return dict(item)
    return None


def create_pull(
    api: GitHubTransport,
    *,
    upstream: str,
    fork: str,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    existing = find_open_pull(api, upstream=upstream, fork=fork, branch=branch)
    if existing is not None:
        return existing
    try:
        created = api.post(
            f"/repos/{upstream}/pulls",
            {
                "title": title,
                "head": pull_head_ref(fork, upstream, branch),
                "base": base,
                "body": body,
            },
        )
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(created, Mapping) or not isinstance(created.get("number"), int):
        raise TransportError("malformed create-pull response")
    return dict(created)


def repository_full_name(value: object) -> str:
    name = _full_name(value)
    if name is None:
        raise TransportError("malformed repository")
    return name


def require_sha(value: object) -> str:
    return require_git_sha(value)


WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})


def collaborator_permission(api: GitHubTransport, repository: str, username: str) -> str:
    """Return GitHub permission for ``username``, or ``none``."""

    if not username:
        return "none"
    try:
        payload = api.get(f"/repos/{repository}/collaborators/{username}/permission")
    except GitHubError as exc:
        if exc.status in (404, 403):
            return "none"
        raise_transport(exc)
        raise
    if not isinstance(payload, Mapping):
        return "none"
    permission = payload.get("permission")
    if isinstance(permission, str) and permission:
        return permission
    return "none"


def has_write_permission(api: GitHubTransport, repository: str, username: str) -> bool:
    return collaborator_permission(api, repository, username) in WRITE_PERMISSIONS


def github_commit_files(
    api: GitHubTransport,
    *,
    repository: str,
    branch: str,
    parent_sha: str,
    files: Mapping[str, str],
    message: str,
) -> str:
    """Create a commit on ``branch`` through the Git Data API. Does not checkout PR code."""

    parent = require_git_sha(parent_sha)
    try:
        commit = api.get(f"/repos/{repository}/git/commits/{parent}")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(commit, Mapping):
        raise TransportError("malformed git commit")
    tree_obj = commit.get("tree")
    base_tree = _sha(tree_obj.get("sha") if isinstance(tree_obj, Mapping) else None)
    entries: list[dict[str, str]] = []
    for path, content in files.items():
        posix = path.replace("\\", "/").lstrip("/")
        if not posix or ".." in posix.split("/"):
            raise TransportError("invalid contribution path")
        encoded = content if content.endswith("\n") else content + "\n"
        try:
            blob = api.post(
                f"/repos/{repository}/git/blobs",
                {"content": encoded, "encoding": "utf-8"},
            )
        except GitHubError as exc:
            raise_transport(exc)
            raise
        blob_sha = _sha(blob.get("sha") if isinstance(blob, Mapping) else None)
        if blob_sha is None:
            raise TransportError("malformed git blob")
        entries.append({"path": posix, "mode": ORDINARY_BLOB_MODE, "type": "blob", "sha": blob_sha})
    tree_body: dict[str, Any] = {"tree": entries}
    if base_tree:
        tree_body["base_tree"] = base_tree
    try:
        tree = api.post(f"/repos/{repository}/git/trees", tree_body)
        tree_sha = _sha(tree.get("sha") if isinstance(tree, Mapping) else None)
        if tree_sha is None:
            raise TransportError("malformed git tree")
        created = api.post(
            f"/repos/{repository}/git/commits",
            {"message": message, "tree": tree_sha, "parents": [parent]},
        )
        new_sha = _sha(created.get("sha") if isinstance(created, Mapping) else None)
        if new_sha is None:
            raise TransportError("malformed git commit")
        api.patch(
            f"/repos/{repository}/git/refs/heads/{branch}",
            {"sha": new_sha, "force": False},
        )
    except GitHubError as exc:
        raise_transport(exc)
        raise
    return new_sha
