#!/usr/bin/env python3
"""Central collection: discover public knowledge sources and prepare proposals.

Trusted default-branch code reads already-public knowledge YAML from the
scaffold parent (numeric repository id) and its accessible public forks,
validates eligible v2 entries with this repository's tools, and optionally
opens candidate PRs *in vaws-knowledge only*.

It never writes to a contributing fork, never clones a fork, never imports
fetched bytes, and never treats a GitHub blob as contributor identity or
technical truth.

    python3 -m vaws_knowledge.sync.collect --mode preview --stash /tmp/vaws-collect --json coverage.json
    python3 -m vaws_knowledge.sync.collect --mode propose --from-exports /tmp/vaws-collect/handoff/success/<token>

Preview never calls git/gh write APIs. Propose reuses sync/plan.py and
sync/propose.py (no --skip-gates / --drop-undeclared / --allow-duplicate-candidates).
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from vaws_knowledge.sync._common import (  # noqa: E402
    EXIT_ERROR,
    EXIT_GATE,
    EXIT_OK,
    SCOPE_KEY_ORDER,
    GateResult,
    Runner,
    SyncError,
    default_runner,
    json_dumps,
    load_corpus,
    load_export,
    repo_root_from,
    run_source_gates,
)
from vaws_knowledge.sync.plan import compute_plan  # noqa: E402
from vaws_knowledge.sync.propose import (  # noqa: E402
    PullRequestResult,
    build_proposal,
    open_pull_request,
)

PARENT_REPOSITORY_ID = 1196723340
KNOWLEDGE_PREFIX_PARTS = (".agents", "knowledge")
ALLOWED_SUFFIXES = (".yaml", ".yml")
ORDINARY_BLOB_MODE = "100644"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "vaws-knowledge-central-collection"

DEFAULT_MAX_PAGES = 5
HARD_MAX_PAGES = 20
DEFAULT_MAX_FORKS = 100
HARD_MAX_FORKS = 500
DEFAULT_MAX_FILES_PER_REPO = 32
HARD_MAX_FILES_PER_REPO = 128
DEFAULT_MAX_FILE_BYTES = 262_144
HARD_MAX_FILE_BYTES = 1_048_576
DEFAULT_MAX_TOTAL_BYTES = 8_388_608
HARD_MAX_TOTAL_BYTES = 32_772_608
DEFAULT_PER_PAGE = 100
HARD_PER_PAGE = 100

FORBIDDEN_FLAGS = ("--skip-gates", "--drop-undeclared", "--allow-duplicate-candidates")

PRIVATE_SOURCE_RECIPE = (
    "Private or unreachable sources remain uninspected. On a local clone of "
    "that source, run this repository's `vaws-knowledge export "
    ".agents/knowledge/*.yaml --origin-repo <owner/repo> -o export.yaml` and "
    "open a candidate PR against vaws-knowledge. Central collection does not "
    "add credentials or expand access."
)

PR_CI_NOTE = (
    "Candidate PRs opened with the built-in GITHUB_TOKEN report pull-request "
    "CI as awaiting any GitHub-required approval; this workflow does not "
    "claim that CI executed or passed, and it does not auto-approve reviews."
)

SCOPE_DIMENSIONS = SCOPE_KEY_ORDER


class GitHubError(Exception):
    def __init__(self, status: int, path: str, detail: str = "") -> None:
        self.status = status
        self.path = path
        super().__init__(f"GitHub API {status} for {path}")


class CollectRefuse(SyncError):
    """Fail closed without proposing."""


class GitHubClient(Protocol):
    def get(self, path: str) -> Any:
        ...

    def paginate(self, path: str, *, max_pages: int) -> tuple[list[Any], bool]:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise GitHubError(int(code), str(getattr(req, "full_url", newurl)), "redirect refused")


def _next_link(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    for part in header.split(","):
        piece = part.strip()
        if 'rel="next"' not in piece:
            continue
        if piece.startswith("<") and ">" in piece:
            url = piece[1 : piece.index(">")]
            if url.startswith(API_ROOT):
                return url[len(API_ROOT) :]
            if url.startswith("/"):
                return url
    return None


class UrllibCollectGitHub:
    """Read-only GitHub client. Tests inject a fake instead of this class."""

    def __init__(self, token: str) -> None:
        if not token:
            raise CollectRefuse("GITHUB_TOKEN is required to read public repository metadata")
        self._token = token

    def _request(self, path: str) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else API_ROOT + path
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        request = urllib.request.Request(url, method="GET", headers=headers)
        opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=60) as response:
                raw = response.read()
                header_map = {key: value for key, value in response.headers.items()}
        except GitHubError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                exc.read(4096)
            except Exception:
                pass
            raise GitHubError(int(exc.code), path) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(0, path) from exc
        payload: Any = None
        if raw:
            payload = json.loads(raw.decode("utf-8"))
        return payload, header_map

    def get(self, path: str) -> Any:
        payload, _headers = self._request(path)
        return payload

    def paginate(self, path: str, *, max_pages: int) -> tuple[list[Any], bool]:
        items: list[Any] = []
        current: Optional[str] = path
        pages = 0
        truncated = False
        while current:
            if pages >= max_pages:
                truncated = True
                break
            payload, headers = self._request(current)
            if not isinstance(payload, list):
                raise CollectRefuse("malformed paginated GitHub listing")
            items.extend(payload)
            pages += 1
            current = _next_link(headers.get("Link") or headers.get("link"))
        return items, truncated


def finite_bound(value: Any, *, default: int, hard_max: int) -> int:
    """Invalid or non-positive values keep the default; they never unbind."""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return hard_max if number > hard_max else number


@dataclasses.dataclass(frozen=True)
class Bounds:
    max_pages: int = DEFAULT_MAX_PAGES
    max_forks: int = DEFAULT_MAX_FORKS
    max_files_per_repo: int = DEFAULT_MAX_FILES_PER_REPO
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    per_page: int = DEFAULT_PER_PAGE

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None = None) -> "Bounds":
        values = values or {}
        return cls(
            max_pages=finite_bound(
                values.get("max_pages"), default=DEFAULT_MAX_PAGES, hard_max=HARD_MAX_PAGES
            ),
            max_forks=finite_bound(
                values.get("max_forks"), default=DEFAULT_MAX_FORKS, hard_max=HARD_MAX_FORKS
            ),
            max_files_per_repo=finite_bound(
                values.get("max_files_per_repo"),
                default=DEFAULT_MAX_FILES_PER_REPO,
                hard_max=HARD_MAX_FILES_PER_REPO,
            ),
            max_file_bytes=finite_bound(
                values.get("max_file_bytes"),
                default=DEFAULT_MAX_FILE_BYTES,
                hard_max=HARD_MAX_FILE_BYTES,
            ),
            max_total_bytes=finite_bound(
                values.get("max_total_bytes"),
                default=DEFAULT_MAX_TOTAL_BYTES,
                hard_max=HARD_MAX_TOTAL_BYTES,
            ),
            per_page=finite_bound(
                values.get("per_page"), default=DEFAULT_PER_PAGE, hard_max=HARD_PER_PAGE
            ),
        )


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _posix_parts(path: str) -> Optional[list[str]]:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        return None
    posix = path.replace("\\", "/")
    parts = [part for part in posix.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def validate_knowledge_path(path: str) -> Optional[str]:
    """Return a rejection reason, or None if the path is an allowed YAML blob path."""
    parts = _posix_parts(path)
    if parts is None:
        return "path_traversal"
    if parts[:2] != list(KNOWLEDGE_PREFIX_PARTS):
        return "outside_knowledge_root"
    if len(parts) < 3:
        return "not_a_file"
    name = parts[-1]
    if name.startswith("."):
        return "hidden"
    if not name.endswith(ALLOWED_SUFFIXES):
        return "not_yaml"
    return None


def validate_selected_corpus_path(path: str) -> Optional[str]:
    """Allowed corpus/examples YAML at a knowledge-repo head. None means allowed."""
    parts = _posix_parts(path)
    if parts is None:
        return "path_traversal"
    if parts[0] == "corpus":
        if len(parts) < 3 or parts[1] not in ("verified", "unverified"):
            return "outside_selected_corpus"
    elif parts[0] == "examples":
        if len(parts) < 2:
            return "outside_selected_corpus"
    else:
        return "outside_selected_corpus"
    name = parts[-1]
    if name.startswith(".") or not name.endswith(ALLOWED_SUFFIXES):
        return "not_yaml"
    return None


def decode_git_blob(payload: Any, expected_sha: str, *, max_bytes: int) -> bytes:
    if not isinstance(payload, Mapping):
        raise CollectRefuse("malformed git blob")
    encoding = payload.get("encoding")
    content = payload.get("content")
    if encoding != "base64" or not isinstance(content, str):
        raise CollectRefuse("git blob is not a base64 ordinary blob")
    try:
        data = base64.b64decode(content, validate=False)
    except (ValueError, TypeError) as exc:
        raise CollectRefuse("git blob could not be decoded") from exc
    if len(data) > max_bytes:
        raise CollectRefuse("oversize")
    recorded = payload.get("sha")
    if isinstance(recorded, str) and recorded and recorded != expected_sha:
        raise CollectRefuse("blob_sha_mismatch")
    if git_blob_sha1(data) != expected_sha:
        raise CollectRefuse("blob_sha_mismatch")
    return data


def _load_yaml_bytes(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return CollectRefuse("not_utf8")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise CollectRefuse("PyYAML is required") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return CollectRefuse("not_yaml")


def _entry_scope_class(entry: Mapping[str, Any]) -> tuple[str, str]:
    if entry.get("schema_version") == 1 or "applicable_versions" in entry:
        return "v1", "v1_entry"
    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        return "incomplete", "missing_scope"
    missing = [dim for dim in SCOPE_DIMENSIONS if dim not in scope]
    if missing:
        return "incomplete", "incomplete_scope"
    for constraint in scope.values():
        if isinstance(constraint, Mapping) and constraint.get("any") is True:
            basis = constraint.get("basis")
            if not isinstance(basis, str) or not basis.strip():
                return "incomplete", "unresolved_dimensions"
    return "eligible", "v2_bounded"


def classify_knowledge_document(data: Any) -> tuple[str, str]:
    """Classify fetched YAML as eligible v2, v1, incomplete, or unknown."""
    if isinstance(data, CollectRefuse):
        return "unknown", str(data)
    if data is None:
        return "unknown", "empty"
    if isinstance(data, Mapping):
        if data.get("schema_version") == 1 or "applicable_versions" in data:
            return "v1", "v1_document"
        version = data.get("schema_version")
        if version not in (None, 2):
            return "unknown", "unknown_schema_version"
        entries = data.get("entries")
        if isinstance(entries, list):
            if not entries:
                return "unknown", "no_entries"
            for item in entries:
                if not isinstance(item, Mapping):
                    return "unknown", "non_mapping_entry"
                status, reason = _entry_scope_class(item)
                if status != "eligible":
                    return status, reason
            return "eligible", "v2_bounded"
        if "uuid" in data:
            return _entry_scope_class(data)
        return "unknown", "not_an_entry_document"
    if isinstance(data, list):
        if not data:
            return "unknown", "no_entries"
        for item in data:
            if not isinstance(item, Mapping):
                return "unknown", "non_mapping_entry"
            status, reason = _entry_scope_class(item)
            if status != "eligible":
                return status, reason
        return "eligible", "v2_bounded"
    return "unknown", "unsupported_shape"


def _repo_id(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _full_name(value: Any) -> Optional[str]:
    if isinstance(value, str) and "/" in value and not value.startswith("/"):
        return value
    if isinstance(value, Mapping):
        return _full_name(value.get("full_name"))
    return None


def _sha(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.lower()
    if len(text) not in (40, 64):
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _coverage_item(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _parent_match(detail: Mapping[str, Any], parent_id: int) -> bool:
    source = detail.get("source") if isinstance(detail.get("source"), Mapping) else None
    parent = detail.get("parent") if isinstance(detail.get("parent"), Mapping) else None
    source_id = _repo_id(source.get("id") if source else None)
    parent_repo_id = _repo_id(parent.get("id") if parent else None)
    if source_id is not None:
        return source_id == parent_id
    if parent_repo_id is not None:
        return parent_repo_id == parent_id
    return False


def resolve_parent(api: GitHubClient, parent_id: int) -> dict[str, Any]:
    payload = api.get(f"/repositories/{parent_id}")
    if not isinstance(payload, Mapping):
        raise CollectRefuse("parent repository lookup returned a malformed body")
    if _repo_id(payload.get("id")) != parent_id:
        raise CollectRefuse("parent repository id did not match the frozen id")
    name = _full_name(payload)
    if name is None:
        raise CollectRefuse("parent repository has no full_name")
    return dict(payload)


def _get_repo(api: GitHubClient, *, repo_id: Optional[int] = None, full_name: Optional[str] = None) -> dict[str, Any]:
    if repo_id is not None:
        payload = api.get(f"/repositories/{repo_id}")
    elif full_name is not None:
        payload = api.get(f"/repos/{full_name}")
    else:
        raise CollectRefuse("repository lookup requires an id")
    if not isinstance(payload, Mapping):
        raise CollectRefuse("repository lookup returned a malformed body")
    return dict(payload)


def freeze_default_commit(api: GitHubClient, full_name: str, default_branch: str) -> tuple[str, str]:
    commit = api.get(f"/repos/{full_name}/commits/{default_branch}")
    if not isinstance(commit, Mapping):
        raise CollectRefuse("malformed commit lookup")
    sha = _sha(commit.get("sha"))
    if sha is None:
        raise CollectRefuse("missing frozen commit sha")
    tree = None
    commit_obj = commit.get("commit")
    if isinstance(commit_obj, Mapping) and isinstance(commit_obj.get("tree"), Mapping):
        tree = _sha(commit_obj["tree"].get("sha"))
    if tree is None and isinstance(commit.get("commit"), Mapping):
        tree = _sha((commit_obj or {}).get("tree") and commit_obj["tree"].get("sha"))  # type: ignore[union-attr]
    if tree is None:
        # Fall back to the commit SHA as a tree-ish; git trees accept commit SHAs.
        tree = sha
    return sha, tree


def list_tree_entries(api: GitHubClient, full_name: str, tree_sha: str) -> tuple[list[dict[str, Any]], bool]:
    payload = api.get(f"/repos/{full_name}/git/trees/{tree_sha}?recursive=1")
    if not isinstance(payload, Mapping):
        raise CollectRefuse("malformed git tree")
    entries = payload.get("tree")
    if not isinstance(entries, list):
        raise CollectRefuse("malformed git tree")
    truncated = bool(payload.get("truncated"))
    return [item for item in entries if isinstance(item, Mapping)], truncated


@dataclasses.dataclass
class FetchedBlob:
    repository_id: int
    full_name: str
    commit_sha: str
    blob_sha: str
    path: str
    data: bytes


def fetch_blob(
    api: GitHubClient,
    full_name: str,
    blob_sha: str,
    *,
    max_bytes: int,
) -> bytes:
    payload = api.get(f"/repos/{full_name}/git/blobs/{blob_sha}")
    return decode_git_blob(payload, blob_sha, max_bytes=max_bytes)


def _stash_file(stash: pathlib.Path, repo_id: int, commit: str, path: str, data: bytes) -> pathlib.Path:
    parts = _posix_parts(path)
    if parts is None:
        raise CollectRefuse("path_traversal")
    dest = stash.joinpath("fetched", str(repo_id), commit, *parts)
    dest.resolve().relative_to((stash / "fetched").resolve())
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def ensure_stash_not_on_path(stash: pathlib.Path) -> None:
    resolved = stash.resolve()
    for item in sys.path:
        if not item:
            continue
        try:
            path = pathlib.Path(item).resolve()
        except OSError:
            continue
        if path == resolved:
            raise CollectRefuse("refusing to proceed: fetched source bytes are on sys.path")
        try:
            path.relative_to(resolved)
        except ValueError:
            continue
        raise CollectRefuse("refusing to proceed: fetched source bytes are on sys.path")


def _record(bucket: list[dict[str, Any]], **fields: Any) -> None:
    bucket.append(_coverage_item(**fields))


def discover_and_fetch(
    api: GitHubClient,
    *,
    parent_id: int,
    bounds: Bounds,
    stash: pathlib.Path,
    path_validator: Callable[[str], Optional[str]] = validate_knowledge_path,
) -> dict[str, Any]:
    """Discover parent+forks, freeze SHAs, fetch allowed blobs into stash."""
    ensure_stash_not_on_path(stash)
    discovered: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    inspected: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    inaccessible: list[dict[str, Any]] = []
    limit_omitted: list[dict[str, Any]] = []
    fetched: list[FetchedBlob] = []
    total_bytes = 0

    try:
        parent = resolve_parent(api, parent_id)
    except GitHubError as exc:
        _record(
            inaccessible,
            repository_id=parent_id,
            reason="parent_lookup_failed",
            status=exc.status,
        )
        return _discovery_result(
            parent_id, None, discovered, selected, inspected, unsupported,
            rejected, inaccessible, limit_omitted, fetched, forks_count=None,
            listed_forks=0, truncated=False,
        )

    parent_name = _full_name(parent)
    parent_default = parent.get("default_branch") if isinstance(parent.get("default_branch"), str) else "main"
    forks_count = parent.get("forks_count") if isinstance(parent.get("forks_count"), int) else None
    _record(
        discovered,
        repository_id=parent_id,
        full_name=parent_name,
        role="parent",
        identity="numeric_id",
    )

    listed_forks = 0
    truncated = False
    try:
        pages, truncated = api.paginate(
            f"/repos/{parent_name}/forks?per_page={bounds.per_page}",
            max_pages=bounds.max_pages,
        )
    except GitHubError as exc:
        _record(
            inaccessible,
            repository_id=parent_id,
            full_name=parent_name,
            reason="fork_list_failed",
            status=exc.status,
        )
        pages = []
        truncated = False

    fork_summaries: list[Mapping[str, Any]] = [item for item in pages if isinstance(item, Mapping)]
    listed_forks = len(fork_summaries)
    if truncated:
        _record(
            limit_omitted,
            reason="max_pages",
            observed_pages=bounds.max_pages,
            listed_so_far=listed_forks,
        )
    if forks_count is not None and listed_forks < forks_count:
        _record(
            inaccessible,
            reason="forks_count_exceeds_accessible_list",
            metadata_forks_count=forks_count,
            accessible_list_count=listed_forks,
            note="observation only; not a fixture and not a 35/35 coverage claim",
        )

    candidates: list[tuple[str, dict[str, Any]]] = [("parent", parent)]
    for index, summary in enumerate(fork_summaries):
        if index >= bounds.max_forks:
            _record(
                limit_omitted,
                repository_id=_repo_id(summary.get("id")),
                full_name=_full_name(summary),
                reason="max_forks",
            )
            continue
        fork_id = _repo_id(summary.get("id"))
        observed_name = _full_name(summary)
        _record(
            discovered,
            repository_id=fork_id,
            full_name=observed_name,
            role="fork_listing",
        )
        if fork_id is None:
            _record(rejected, full_name=observed_name, reason="missing_repository_id")
            continue
        try:
            detail = _get_repo(api, repo_id=fork_id)
        except GitHubError as exc:
            _record(
                inaccessible,
                repository_id=fork_id,
                full_name=observed_name,
                reason="repository_lookup_failed",
                status=exc.status,
            )
            continue
        candidates.append(("fork", detail))

    for role, detail in candidates:
        repo_id = _repo_id(detail.get("id"))
        full_name = _full_name(detail)
        if repo_id is None or full_name is None:
            _record(rejected, role=role, reason="identity_unproven")
            continue
        if detail.get("private") is True:
            _record(
                inaccessible,
                repository_id=repo_id,
                full_name=full_name,
                reason="private",
                recipe="local-export",
            )
            continue
        if role == "parent":
            if repo_id != parent_id:
                _record(rejected, repository_id=repo_id, full_name=full_name, reason="parent_id_mismatch")
                continue
        else:
            if not _parent_match(detail, parent_id):
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    reason="wrong_ancestry",
                )
                continue
        if detail.get("archived") is True or detail.get("disabled") is True:
            _record(
                unsupported,
                repository_id=repo_id,
                full_name=full_name,
                reason="archived_or_disabled",
            )
            continue
        default_branch = detail.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            default_branch = parent_default
        try:
            commit_sha, tree_sha = freeze_default_commit(api, full_name, default_branch)
        except GitHubError as exc:
            _record(
                inaccessible,
                repository_id=repo_id,
                full_name=full_name,
                reason="commit_freeze_failed",
                status=exc.status,
            )
            continue
        except CollectRefuse as exc:
            _record(rejected, repository_id=repo_id, full_name=full_name, reason=str(exc))
            continue
        _record(
            selected,
            repository_id=repo_id,
            full_name=full_name,
            role=role,
            commit_sha=commit_sha,
            default_branch=default_branch,
        )
        try:
            entries, tree_truncated = list_tree_entries(api, full_name, tree_sha)
        except GitHubError as exc:
            _record(
                inaccessible,
                repository_id=repo_id,
                full_name=full_name,
                commit_sha=commit_sha,
                reason="tree_list_failed",
                status=exc.status,
            )
            continue
        except CollectRefuse as exc:
            _record(
                rejected,
                repository_id=repo_id,
                full_name=full_name,
                commit_sha=commit_sha,
                reason=str(exc),
            )
            continue
        _record(
            inspected,
            repository_id=repo_id,
            full_name=full_name,
            commit_sha=commit_sha,
        )
        if tree_truncated:
            _record(
                limit_omitted,
                repository_id=repo_id,
                full_name=full_name,
                commit_sha=commit_sha,
                reason="truncated_tree",
            )
        knowledge_entries = []
        for entry in entries:
            path = entry.get("path")
            if not isinstance(path, str):
                continue
            reason = path_validator(path)
            if reason == "outside_knowledge_root" or reason == "outside_selected_corpus" or reason == "not_yaml" or reason == "not_a_file" or reason == "hidden":
                continue
            knowledge_entries.append(entry)
        if len(knowledge_entries) > bounds.max_files_per_repo:
            extra = knowledge_entries[bounds.max_files_per_repo :]
            knowledge_entries = knowledge_entries[: bounds.max_files_per_repo]
            for entry in extra:
                _record(
                    limit_omitted,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=entry.get("path"),
                    reason="max_files_per_repo",
                )
        for entry in knowledge_entries:
            path = str(entry.get("path"))
            reason = path_validator(path)
            if reason is not None:
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason=reason,
                )
                continue
            mode = entry.get("mode")
            entry_type = entry.get("type")
            blob_sha = _sha(entry.get("sha"))
            if entry_type != "blob" or blob_sha is None:
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="not_ordinary_blob",
                )
                continue
            if mode == "120000":
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="symlink",
                )
                continue
            if mode == "160000":
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="submodule",
                )
                continue
            if mode == "100755":
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="executable",
                )
                continue
            if mode != ORDINARY_BLOB_MODE:
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="not_ordinary_blob",
                )
                continue
            size = entry.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size > bounds.max_file_bytes:
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    blob_sha=blob_sha,
                    reason="oversize",
                )
                continue
            if total_bytes >= bounds.max_total_bytes:
                _record(
                    limit_omitted,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="max_total_bytes",
                )
                continue
            try:
                data = fetch_blob(api, full_name, blob_sha, max_bytes=bounds.max_file_bytes)
            except GitHubError as exc:
                _record(
                    inaccessible,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    blob_sha=blob_sha,
                    reason="blob_fetch_failed",
                    status=exc.status,
                )
                continue
            except CollectRefuse as exc:
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    blob_sha=blob_sha,
                    reason=str(exc),
                )
                continue
            if total_bytes + len(data) > bounds.max_total_bytes:
                _record(
                    limit_omitted,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="max_total_bytes",
                )
                continue
            try:
                stored = _stash_file(stash, repo_id, commit_sha, path, data)
            except (CollectRefuse, ValueError):
                _record(
                    rejected,
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    path=path,
                    reason="stash_escape",
                )
                continue
            total_bytes += len(data)
            fetched.append(
                FetchedBlob(
                    repository_id=repo_id,
                    full_name=full_name,
                    commit_sha=commit_sha,
                    blob_sha=blob_sha,
                    path=path,
                    data=data,
                )
            )
            stored.write_bytes(data)

    return _discovery_result(
        parent_id,
        parent_name,
        discovered,
        selected,
        inspected,
        unsupported,
        rejected,
        inaccessible,
        limit_omitted,
        fetched,
        forks_count=forks_count,
        listed_forks=listed_forks,
        truncated=truncated,
    )


def _discovery_result(
    parent_id: int,
    parent_name: Optional[str],
    discovered: list,
    selected: list,
    inspected: list,
    unsupported: list,
    rejected: list,
    inaccessible: list,
    limit_omitted: list,
    fetched: list[FetchedBlob],
    *,
    forks_count: Optional[int],
    listed_forks: int,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "parent": {
            "id": parent_id,
            "full_name": parent_name,
            "identity": "numeric_id",
            "metadata_forks_count": forks_count,
            "accessible_list_count": listed_forks,
        },
        "coverage": {
            "discovered": discovered,
            "selected": selected,
            "inspected": inspected,
            "unsupported": unsupported,
            "rejected": rejected,
            "inaccessible": inaccessible,
            "limit_omitted": limit_omitted,
        },
        "fetched": fetched,
        "truncated_listing": truncated,
        "private_source_recipe": PRIVATE_SOURCE_RECIPE,
    }


@dataclasses.dataclass
class Observation:
    uuid: str
    content_hash: Optional[str]
    kind: str
    classification: str
    reason: str
    bindings: list[dict[str, Any]]
    export_path: Optional[pathlib.Path] = None
    entry: Optional[dict[str, Any]] = None


def _entry_kind(doc: Any, fallback: str) -> str:
    if isinstance(doc, Mapping) and isinstance(doc.get("kind"), str) and doc["kind"]:
        return doc["kind"]
    return fallback


def _entries_from(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, Mapping) and isinstance(doc.get("entries"), list):
        return [item for item in doc["entries"] if isinstance(item, dict)]
    if isinstance(doc, Mapping) and "uuid" in doc:
        return [dict(doc)]
    if isinstance(doc, list):
        return [item for item in doc if isinstance(item, dict)]
    return []


def _run_export(
    tools_dir: pathlib.Path | None,
    source: pathlib.Path,
    dest: pathlib.Path,
    origin_repo: str,
    *,
    kind: Optional[str],
    runner: Runner,
    python: str,
    cwd: pathlib.Path,
) -> GateResult:
    if tools_dir is not None:
        script = pathlib.Path(tools_dir) / "export.py"
        if not script.is_file():
            return GateResult("export.py", "unavailable", "gate unavailable: export.py is missing")
        prefix = [python, str(script)]
    else:
        prefix = [python, "-m", "vaws_knowledge", "export"]
    cmd = [
        *prefix,
        str(source),
        "--origin-repo",
        origin_repo,
        "-o",
        str(dest),
    ]
    if kind:
        cmd.extend(["--kind", kind])
    for flag in FORBIDDEN_FLAGS:
        if flag in cmd:
            raise CollectRefuse(f"refusing forbidden flag {flag}")
    proc = runner(cmd, cwd=str(cwd))
    if proc.returncode == 0:
        return GateResult("export.py", "passed")
    return GateResult("export.py", "failed", f"exit {proc.returncode}")


def prepare_exports(
    fetched: Sequence[FetchedBlob],
    *,
    stash: pathlib.Path,
    tools_dir: pathlib.Path,
    repo: pathlib.Path,
    runner: Runner,
    python: str,
    coverage: dict[str, list],
) -> tuple[list[pathlib.Path], list[Observation]]:
    ensure_stash_not_on_path(stash)
    exports_dir = stash / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    observations: list[Observation] = []
    export_paths: list[pathlib.Path] = []

    for index, blob in enumerate(sorted(fetched, key=lambda item: (item.repository_id, item.path, item.blob_sha))):
        parsed = _load_yaml_bytes(blob.data)
        classification, reason = classify_knowledge_document(parsed)
        binding = {
            "repository_id": blob.repository_id,
            "full_name": blob.full_name,
            "commit_sha": blob.commit_sha,
            "blob_sha": blob.blob_sha,
            "path": blob.path,
            "retrieval": "github-git-blob",
            "note": "A public GitHub blob proves where bytes were read, not contributor identity or technical truth.",
        }
        if classification != "eligible":
            bucket = "unsupported" if classification in {"v1", "incomplete", "unknown"} else "rejected"
            _record(
                coverage[bucket],
                repository_id=blob.repository_id,
                full_name=blob.full_name,
                commit_sha=blob.commit_sha,
                path=blob.path,
                blob_sha=blob.blob_sha,
                classification=classification,
                reason=reason,
            )
            observations.append(
                Observation(
                    uuid="",
                    content_hash=None,
                    kind="",
                    classification=classification,
                    reason=reason,
                    bindings=[binding],
                )
            )
            continue
        source_path = _stash_file(stash, blob.repository_id, blob.commit_sha, blob.path, blob.data)
        dest = exports_dir / f"{blob.repository_id}-{index}-{pathlib.Path(blob.path).name}"
        fallback_kind = pathlib.Path(blob.path).stem
        kind = _entry_kind(parsed, fallback_kind)
        gate = _run_export(
            tools_dir,
            source_path,
            dest,
            blob.full_name,
            kind=kind if not (isinstance(parsed, Mapping) and parsed.get("kind")) else None,
            runner=runner,
            python=python,
            cwd=repo,
        )
        if not gate.ok or not dest.is_file():
            _record(
                coverage["rejected"],
                repository_id=blob.repository_id,
                full_name=blob.full_name,
                commit_sha=blob.commit_sha,
                path=blob.path,
                blob_sha=blob.blob_sha,
                reason="export_or_gate_failed",
            )
            continue
        result_gates = run_source_gates(tools_dir, [dest], runner=runner, skip=False)
        if not all(item.ok for item in result_gates):
            _record(
                coverage["rejected"],
                repository_id=blob.repository_id,
                full_name=blob.full_name,
                commit_sha=blob.commit_sha,
                path=blob.path,
                blob_sha=blob.blob_sha,
                reason="mandatory_gates_failed",
            )
            continue
        try:
            import yaml
            exported = yaml.safe_load(dest.read_text(encoding="utf-8"))
        except Exception:
            _record(
                coverage["rejected"],
                repository_id=blob.repository_id,
                full_name=blob.full_name,
                path=blob.path,
                reason="export_unreadable",
            )
            continue
        export_paths.append(dest)
        for entry in _entries_from(exported):
            observations.append(
                Observation(
                    uuid=str(entry.get("uuid") or ""),
                    content_hash=entry.get("content_hash") if isinstance(entry.get("content_hash"), str) else None,
                    kind=str((exported or {}).get("kind") or kind),
                    classification="eligible",
                    reason="v2_bounded",
                    bindings=[binding],
                    export_path=dest,
                    entry=entry,
                )
            )
    return export_paths, observations


_HASHED_ENTRY_FIELDS = frozenset({"uuid", "content_hash", "scope", "rule", "measurement"})


def _non_hash_metadata(entry: Mapping[str, Any] | None, kind: str) -> str:
    """Kind plus non-hashed claim/provenance/verification/lifecycle fields."""
    payload: dict[str, Any] = {"kind": kind}
    if isinstance(entry, Mapping):
        for key, value in entry.items():
            if key not in _HASHED_ENTRY_FIELDS:
                payload[key] = value
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)


def deduplicate_observations(observations: Sequence[Observation]) -> tuple[list[Observation], list[dict[str, Any]]]:
    """Identical uuid+hash+metadata collapse; incompatible metadata is a conflict."""
    by_uuid: dict[str, list[Observation]] = {}
    passthrough: list[Observation] = []
    for item in observations:
        if item.classification != "eligible" or not item.uuid or not item.content_hash:
            passthrough.append(item)
            continue
        by_uuid.setdefault(item.uuid, []).append(item)

    unique: list[Observation] = []
    conflicts: list[dict[str, Any]] = []
    for uuid, group in sorted(by_uuid.items()):
        hashes = {item.content_hash for item in group}
        bindings = []
        for item in group:
            bindings.extend(item.bindings)
        if len(hashes) > 1:
            conflicts.append(
                {
                    "uuid": uuid,
                    "content_hashes": sorted(h for h in hashes if h),
                    "bindings": bindings,
                    "reason": "conflicting_revision",
                    "action": "reported_not_chosen",
                }
            )
            continue
        kinds = {item.kind for item in group}
        metas = {_non_hash_metadata(item.entry, item.kind) for item in group}
        if len(kinds) > 1 or len(metas) > 1:
            conflicts.append(
                {
                    "uuid": uuid,
                    "content_hashes": sorted(h for h in hashes if h),
                    "bindings": bindings,
                    "reason": "incompatible_metadata",
                    "action": "reported_not_chosen",
                }
            )
            continue
        primary = sorted(group, key=lambda item: (item.bindings[0]["repository_id"], item.bindings[0]["path"]))[0]
        unique.append(
            Observation(
                uuid=primary.uuid,
                content_hash=primary.content_hash,
                kind=primary.kind,
                classification="eligible",
                reason="v2_bounded",
                bindings=bindings,
                export_path=primary.export_path,
                entry=primary.entry,
            )
        )
    return unique + passthrough, conflicts


def write_deduped_exports(unique: Sequence[Observation], stash: pathlib.Path) -> list[pathlib.Path]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise CollectRefuse("PyYAML is required") from exc
    by_kind: dict[str, list[Observation]] = {}
    for item in unique:
        if item.classification != "eligible" or item.entry is None:
            continue
        by_kind.setdefault(item.kind or "known-failure-signatures", []).append(item)
    paths: list[pathlib.Path] = []
    token = secrets.token_hex(8)
    out_dir = stash / "exports" / "assembled" / token
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, group in sorted(by_kind.items()):
        entries = [dict(item.entry) for item in group if item.entry is not None]
        if not entries:
            continue
        updated = []
        for entry in entries:
            lifecycle = entry.get("lifecycle") if isinstance(entry.get("lifecycle"), dict) else {}
            if isinstance(lifecycle.get("updated_at"), str):
                updated.append(lifecycle["updated_at"])
        doc = {
            "schema_version": 2,
            "kind": kind,
            "layer": "unverified",
            "updated_at": max(updated) if updated else "1970-01-01",
            "entries": entries,
        }
        path = out_dir / f"{kind}.yaml"
        path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def _plan_conflicts(plan) -> list[dict[str, Any]]:
    items = list(plan.by_action("conflict")) + list(plan.by_action("duplicate-candidate"))
    return [item.to_public() for item in items]


def propose_exports(
    export_paths: Sequence[pathlib.Path],
    *,
    repo: pathlib.Path,
    tools_dir: pathlib.Path,
    mode: str,
    runner: Runner,
    open_pr: Callable[..., PullRequestResult] | None = None,
    remote: str = "origin",
    base: str = "main",
    day: Optional[str] = None,
) -> dict[str, Any]:
    empty = {
        "wrote": False,
        "pr_ci": PR_CI_NOTE,
        "conflicts": [],
        "plan": None,
    }
    if not export_paths:
        return {
            **empty,
            "status": "nothing-to-propose",
            "detail": "zero eligible entries is an honest no-op",
        }
    gates = run_source_gates(tools_dir, list(export_paths), runner=runner, skip=False)
    if not all(gate.ok for gate in gates):
        return {
            **empty,
            "status": "failed",
            "detail": "mandatory gates failed; prior corpus state is unchanged",
            "gates": [{"name": g.name, "status": g.status} for g in gates],
        }
    corpus_dir = pathlib.Path(repo) / "corpus"
    local_plan = compute_plan([load_export(p) for p in export_paths], load_corpus(corpus_dir), day=day)
    local_conflicts = _plan_conflicts(local_plan)
    planned = {
        "conflicts": local_conflicts,
        "plan": local_plan.to_public(),
        "pr_ci": PR_CI_NOTE,
        "plan_source": "local-checkout",
    }
    if mode != "propose":
        detail = "preview mode never writes branches or pull requests"
        if local_conflicts:
            detail = "preview; planner reported conflicts or duplicate candidates against the local checkout"
        return {
            **planned,
            "status": "preview",
            "wrote": False,
            "detail": detail,
        }
    opener = open_pr or open_pull_request
    opener_kwargs = {
        "repo": repo,
        "tools_dir": tools_dir,
        "remote": remote,
        "base": base,
        "day": day,
        "allow_duplicate_candidates": False,
        "runner": runner,
        "log": lambda *_: None,
        "central_collection": True,
    }
    try:
        result = opener(list(export_paths), **opener_kwargs)
    except TypeError:
        opener_kwargs.pop("central_collection", None)
        result = opener(list(export_paths), **opener_kwargs)
    fresh_plan = getattr(result, "plan", None)
    fresh_conflicts = getattr(result, "conflicts", None)
    if fresh_plan is not None:
        planned = {
            "conflicts": list(fresh_conflicts or []),
            "plan": fresh_plan,
            "pr_ci": PR_CI_NOTE,
            "plan_source": "fresh-main",
        }
    status = result.status
    if status == "nothing-to-propose" and planned["conflicts"]:
        status = "conflicts"
    return {
        **planned,
        "status": status,
        "wrote": result.status == "created",
        "branch": result.branch,
        "url": result.url,
        "detail": result.detail,
    }


def public_coverage(result: Mapping[str, Any]) -> dict[str, Any]:
    coverage = result.get("coverage") or {}
    counts = {key: len(coverage.get(key) or []) for key in (
        "discovered", "selected", "inspected", "unsupported", "rejected",
        "inaccessible", "limit_omitted",
    )}
    parent = result.get("parent") or {}
    return {
        "parent": parent,
        "counts": counts,
        "coverage": coverage,
        "conflicts": result.get("conflicts") or [],
        "observations": result.get("observations") or [],
        "eligible_count": result.get("eligible_count", 0),
        "export_paths": result.get("export_paths") or [],
        "has_successful_exports": bool(result.get("export_paths")),
        "proposal": result.get("proposal"),
        "plan": result.get("plan") or (result.get("proposal") or {}).get("plan"),
        "private_source_recipe": PRIVATE_SOURCE_RECIPE,
        "pr_ci": PR_CI_NOTE,
        "notes": [
            "A public GitHub blob proves where bytes were read, not contributor identity or technical truth.",
            "Central re-scan does not prove that a source-side scan occurred.",
            "Zero eligible entries is an honest no-op.",
            "Metadata forks_count vs accessible list length is an observation, not a coverage claim.",
        ],
    }


CURRENT_SUCCESS_MANIFEST = "current-success.json"


def write_handoff(handoff_dir: pathlib.Path, public: Mapping[str, Any], export_paths: Sequence[pathlib.Path]) -> None:
    """Write this run's sanitized result and an isolated current-success export dir.

    Prior generated files are not removed. Downstream jobs must read only the
    directory named in current-success.json; assembled/failed staging is not
    current successful output.
    """
    handoff_dir = pathlib.Path(handoff_dir)
    token = secrets.token_hex(8)
    relative = f"success/{token}"
    dest = handoff_dir / "success" / token
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for path in export_paths:
        name = pathlib.Path(path).name
        shutil.copy2(path, dest / name)
        names.append(name)
    manifest = {
        "directory": relative,
        "files": names,
        "eligible": bool(names),
    }
    (handoff_dir / CURRENT_SUCCESS_MANIFEST).write_text(json_dumps(manifest), encoding="utf-8")
    (handoff_dir / "CURRENT_SUCCESS_DIR").write_text(relative + "\n", encoding="utf-8")
    payload = dict(public)
    payload["current_success"] = manifest
    (handoff_dir / "result.json").write_text(json_dumps(payload), encoding="utf-8")


def run_collection(
    *,
    mode: str,
    parent_id: int = PARENT_REPOSITORY_ID,
    stash: pathlib.Path,
    repo: pathlib.Path,
    tools_dir: pathlib.Path | None,
    api: GitHubClient | None = None,
    token: Optional[str] = None,
    bounds: Bounds | None = None,
    runner: Runner = default_runner,
    python: str = sys.executable,
    open_pr: Callable[..., PullRequestResult] | None = None,
    from_exports: Optional[pathlib.Path] = None,
    remote: str = "origin",
    base: str = "main",
    day: Optional[str] = None,
    handoff_dir: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    if mode not in {"preview", "propose"}:
        raise CollectRefuse("mode must be preview or propose")
    stash = pathlib.Path(stash).resolve()
    stash.mkdir(parents=True, exist_ok=True)
    repo = pathlib.Path(repo).resolve()
    tools_dir = pathlib.Path(tools_dir).resolve() if tools_dir is not None else None
    try:
        stash.relative_to(repo)
        raise CollectRefuse("stash must be outside the trusted checkout")
    except ValueError:
        pass
    except CollectRefuse:
        raise
    ensure_stash_not_on_path(stash)
    bounds = bounds or Bounds()
    coverage_buckets = {
        "discovered": [],
        "selected": [],
        "inspected": [],
        "unsupported": [],
        "rejected": [],
        "inaccessible": [],
        "limit_omitted": [],
    }

    if from_exports is not None:
        export_dir = pathlib.Path(from_exports)
        export_paths = sorted(
            path for path in export_dir.rglob("*")
            if path.is_file() and path.suffix in ALLOWED_SUFFIXES
        )
        discovery = {
            "parent": {"id": parent_id, "identity": "numeric_id"},
            "coverage": coverage_buckets,
            "fetched": [],
            "private_source_recipe": PRIVATE_SOURCE_RECIPE,
        }
        unique: list[Observation] = []
        proposal = propose_exports(
            export_paths,
            repo=repo,
            tools_dir=tools_dir,
            mode=mode,
            runner=runner,
            open_pr=open_pr,
            remote=remote,
            base=base,
            day=day,
        )
        out = {
            **discovery,
            "conflicts": list(proposal.get("conflicts") or []),
            "eligible_count": len(export_paths),
            "export_paths": [str(path) for path in export_paths],
            "proposal": proposal,
            "plan": proposal.get("plan"),
            "observations": [],
        }
        if handoff_dir is not None:
            write_handoff(handoff_dir, public_coverage(out), export_paths)
        return out

    if api is None:
        if not token:
            raise CollectRefuse("GITHUB_TOKEN is required unless a GitHub client is injected")
        api = UrllibCollectGitHub(token)

    discovery = discover_and_fetch(
        api, parent_id=parent_id, bounds=bounds, stash=stash
    )
    coverage_buckets = discovery["coverage"]
    _export_paths, observations = prepare_exports(
        discovery["fetched"],
        stash=stash,
        tools_dir=tools_dir,
        repo=repo,
        runner=runner,
        python=python,
        coverage=coverage_buckets,
    )
    unique, conflicts = deduplicate_observations(observations)
    for item in conflicts:
        _record(
            coverage_buckets["rejected"],
            uuid=item.get("uuid"),
            reason=item.get("reason") or "conflicting_revision",
        )
    export_paths = write_deduped_exports(unique, stash)
    if export_paths:
        gates = run_source_gates(tools_dir, export_paths, runner=runner, skip=False)
        if not all(gate.ok for gate in gates):
            _record(coverage_buckets["rejected"], reason="mandatory_gates_failed")
            # Leave failed assembled YAML in the isolated assembled token
            # directory; it is not current successful handoff output.
            export_paths = []
    eligible_count = sum(1 for item in unique if item.classification == "eligible")
    proposal = propose_exports(
        export_paths,
        repo=repo,
        tools_dir=tools_dir,
        mode=mode,
        runner=runner,
        open_pr=open_pr,
        remote=remote,
        base=base,
        day=day,
    )
    merged_conflicts = list(conflicts) + list(proposal.get("conflicts") or [])
    out = {
        "parent": discovery["parent"],
        "coverage": coverage_buckets,
        "conflicts": merged_conflicts,
        "eligible_count": eligible_count,
        "export_paths": [str(path) for path in export_paths],
        "proposal": proposal,
        "plan": proposal.get("plan"),
        "truncated_listing": discovery.get("truncated_listing"),
        "private_source_recipe": PRIVATE_SOURCE_RECIPE,
        "observations": [
            {
                "uuid": item.uuid,
                "content_hash": item.content_hash,
                "kind": item.kind,
                "classification": item.classification,
                "reason": item.reason,
                "bindings": item.bindings,
            }
            for item in unique
            if item.classification == "eligible"
        ],
    }
    if handoff_dir is not None:
        write_handoff(handoff_dir, public_coverage(out), export_paths)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("preview", "propose"), required=True)
    parser.add_argument("--parent-id", type=int, default=PARENT_REPOSITORY_ID)
    parser.add_argument("--stash", type=pathlib.Path, required=True)
    parser.add_argument("--repo", type=pathlib.Path, default=None)
    parser.add_argument("--tools-dir", type=pathlib.Path, default=None)
    parser.add_argument("--json", type=pathlib.Path, default=None)
    parser.add_argument("--from-exports", type=pathlib.Path, default=None)
    parser.add_argument("--handoff-dir", type=pathlib.Path, default=None)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--today", default=None)
    parser.add_argument("--max-pages", default=None)
    parser.add_argument("--max-forks", default=None)
    parser.add_argument("--max-files-per-repo", default=None)
    parser.add_argument("--max-file-bytes", default=None)
    parser.add_argument("--max-total-bytes", default=None)
    args = parser.parse_args(argv)

    repo = repo_root_from(args.repo)
    tools_dir = pathlib.Path(args.tools_dir).resolve() if args.tools_dir else None
    bounds = Bounds.from_mapping(
        {
            "max_pages": args.max_pages,
            "max_forks": args.max_forks,
            "max_files_per_repo": args.max_files_per_repo,
            "max_file_bytes": args.max_file_bytes,
            "max_total_bytes": args.max_total_bytes,
        }
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        result = run_collection(
            mode=args.mode,
            parent_id=args.parent_id,
            stash=args.stash,
            repo=repo,
            tools_dir=tools_dir,
            token=token,
            bounds=bounds,
            from_exports=args.from_exports,
            remote=args.remote,
            base=args.base,
            day=args.today,
            handoff_dir=args.handoff_dir,
        )
    except CollectRefuse as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    public = public_coverage(result)
    text = json_dumps(public)
    if args.handoff_dir is not None:
        export_paths = [pathlib.Path(p) for p in result.get("export_paths") or []]
        write_handoff(args.handoff_dir, public, export_paths)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    proposal = result.get("proposal") or {}
    if proposal.get("status") == "failed":
        return EXIT_GATE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
