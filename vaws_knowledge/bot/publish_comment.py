"""Publish one bot-owned review comment from a trusted workflow_run.

The unprivileged pull-request workflow may upload anything. This helper
treats the artifact as data: it validates ``gate-results.json``, re-renders
the comment with the trusted ``bot.report`` renderer, and binds the visible
text to the triggering run and the current pull-request head taken from
trusted event/API metadata.

Artifact ``pr-number.txt``, the markdown filename, and the report marker
are not authority. A human comment that happens to start with the marker
is not ownership. Unknown, missing, malformed, mismatched, unrelated-event
and stale-head input produces no write.

This module is stdlib plus ``bot.report``. The publisher job must not
install pull-request dependencies or execute artifact files.

Usage::

    python3 bot/publish_comment.py --artifact-dir review-report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from vaws_knowledge.bot.report import MARKER, render_markdown

EXPECTED_WORKFLOW_NAME = "Review gates"
EXPECTED_WORKFLOW_PATH = ".github/workflows/pr-review.yml"
EXPECTED_ARTIFACT_NAME = "review-report"
EXPECTED_EVENT = "pull_request"
PUBLISHER_WORKFLOW_NAME = "Review comment"
BOT_LOGIN = "github-actions[bot]"
PASS_PERMITS = "corpus/unverified/ only (bot approval never establishes truth)"
FAIL_PERMITS = "nothing"
MAX_REPORT_BYTES = 1_048_576
API_VERSION = "2022-11-28"
API_ROOT = "https://api.github.com"

BINDING_RE = re.compile(
    r"^<!-- vaws-knowledge-review-binding "
    r"run=(?P<run>[0-9]+) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"repo=(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    r"pr=(?P<pr>[0-9]+) -->$"
)


class Refuse(Exception):
    """Fail closed: do not create or update a comment."""


class GitHubError(Exception):
    """A GitHub API call failed."""

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        self.status = status
        self.path = path
        super().__init__(f"GitHub API {status} for {path}: {detail}".rstrip())


class GitHubAPI(Protocol):
    def get(self, path: str) -> Any:
        ...

    def paginate(self, path: str) -> list[Any]:
        ...

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        ...

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        ...


@dataclass(frozen=True)
class Binding:
    run: int
    head: str
    repo: str
    pr: int


@dataclass(frozen=True)
class Outcome:
    wrote: bool
    action: str
    reason: str
    comment_id: Optional[int] = None
    pr: Optional[int] = None


def _sha(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) != 40:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value.lower()


def _full_name(value: Any) -> Optional[str]:
    if isinstance(value, str) and "/" in value and not value.startswith("/"):
        return value
    if isinstance(value, Mapping):
        name = value.get("full_name")
        if isinstance(name, str) and "/" in name:
            return name
    return None


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


def parse_binding(body: str) -> Optional[Binding]:
    for line in body.splitlines():
        match = BINDING_RE.match(line.strip())
        if not match:
            continue
        return Binding(
            run=int(match.group("run")),
            head=match.group("head").lower(),
            repo=match.group("repo"),
            pr=int(match.group("pr")),
        )
    return None


def bind_published_markdown(
    markdown: str,
    *,
    run_id: int,
    head_sha: str,
    repository: str,
    pr: int,
) -> str:
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != MARKER:
        raise Refuse("rendered report lost its marker")
    binding = (
        f"<!-- vaws-knowledge-review-binding run={run_id} head={head_sha} "
        f"repo={repository} pr={pr} -->"
    )
    url = f"https://github.com/{repository}/actions/runs/{run_id}"
    footer = (
        f"_Source: [run {run_id}]({url}) at `{head_sha}` "
        f"(PR #{pr} current head)._"
    )
    body = "\n".join([MARKER, binding, *lines[1:]])
    return body.rstrip() + "\n\n" + footer + "\n"


def validate_report(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise Refuse("malformed report")
    if payload.get("bot") != "vaws-knowledge-review-bot":
        raise Refuse("malformed report")
    if payload.get("report_version") != 1:
        raise Refuse("malformed report")
    if payload.get("mode") != "pr":
        raise Refuse("malformed report")
    overall = payload.get("overall")
    if overall not in {"pass", "fail"}:
        raise Refuse("malformed report")
    permits = payload.get("permits")
    if overall == "pass":
        if permits != PASS_PERMITS:
            raise Refuse("invalid report cannot generate a success comment")
    elif permits != FAIL_PERMITS:
        raise Refuse("malformed report")
    paths = payload.get("paths")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise Refuse("malformed report")
    as_of = payload.get("as_of")
    if not isinstance(as_of, str) or not as_of:
        raise Refuse("malformed report")
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        raise Refuse("malformed report")
    for key in ("gates", "passed", "warned", "failed"):
        value = counts.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise Refuse("malformed report")
    gates = payload.get("gates")
    if not isinstance(gates, list) or not gates:
        raise Refuse("malformed report")
    passed = 0
    warned = 0
    blocking_failed = 0
    for gate in gates:
        if not isinstance(gate, dict):
            raise Refuse("malformed report")
        for key in ("id", "title", "status", "summary"):
            value = gate.get(key)
            if not isinstance(value, str) or not value:
                raise Refuse("malformed report")
        if not isinstance(gate.get("blocking"), bool):
            raise Refuse("malformed report")
        if gate["status"] not in {
            "pass",
            "warn",
            "fail",
            "unavailable",
            "error",
            "skipped",
        }:
            raise Refuse("malformed report")
        details = gate.get("details", [])
        if details is None:
            details = []
        if not isinstance(details, list) or not all(isinstance(item, str) for item in details):
            raise Refuse("malformed report")
        if gate["status"] == "pass":
            passed += 1
        elif gate["status"] == "warn":
            warned += 1
        if gate["blocking"] and gate["status"] not in {"pass", "warn"}:
            blocking_failed += 1
    if counts["gates"] != len(gates):
        raise Refuse("malformed report")
    if counts["passed"] != passed or counts["warned"] != warned:
        raise Refuse("malformed report")
    if counts["failed"] != blocking_failed:
        raise Refuse("malformed report")
    computed = "fail" if blocking_failed else "pass"
    if overall != computed:
        raise Refuse("invalid report cannot generate a success comment")


def load_report(artifact_dir: Path) -> dict[str, Any]:
    path = artifact_dir / "gate-results.json"
    if not path.is_file():
        raise Refuse("missing report")
    data = path.read_bytes()
    if len(data) > MAX_REPORT_BYTES:
        raise Refuse("malformed report")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refuse("malformed report") from exc
    validate_report(payload)
    return payload


def _trusted_run(event: Mapping[str, Any], repository: str) -> dict[str, Any]:
    workflow_run = event.get("workflow_run")
    if not isinstance(workflow_run, Mapping):
        raise Refuse("missing trusted workflow_run")
    run_repo = _full_name(workflow_run.get("repository"))
    if run_repo != repository:
        raise Refuse("wrong repository")
    if workflow_run.get("name") != EXPECTED_WORKFLOW_NAME:
        raise Refuse("wrong workflow")
    if workflow_run.get("path") != EXPECTED_WORKFLOW_PATH:
        raise Refuse("wrong workflow path")
    if workflow_run.get("event") != EXPECTED_EVENT:
        raise Refuse("unrelated event")
    run_id = workflow_run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise Refuse("missing trusted run id")
    head_sha = _sha(workflow_run.get("head_sha"))
    if head_sha is None:
        raise Refuse("missing source revision")
    conclusion = workflow_run.get("conclusion")
    if conclusion not in {"success", "failure"}:
        raise Refuse("unrelated event")
    return {
        "id": run_id,
        "head_sha": head_sha,
        "pull_requests": workflow_run.get("pull_requests") or [],
    }


def _candidate_numbers(workflow_run: Mapping[str, Any], repository: str, source_sha: str, api: GitHubAPI) -> dict[int, Optional[str]]:
    candidates: dict[int, Optional[str]] = {}
    event_prs = workflow_run.get("pull_requests") or []
    if not isinstance(event_prs, list):
        raise Refuse("malformed trusted pull_requests")
    for item in event_prs:
        if not isinstance(item, Mapping):
            continue
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        base_repo = _full_name((item.get("base") or {}).get("repo") if isinstance(item.get("base"), Mapping) else None)
        if base_repo and base_repo != repository:
            continue
        head = None
        if isinstance(item.get("head"), Mapping):
            head = _sha(item["head"].get("sha"))
        candidates[number] = head
    try:
        associated = api.get(f"/repos/{repository}/commits/{source_sha}/pulls")
    except GitHubError:
        associated = []
    if associated is None:
        associated = []
    if not isinstance(associated, list):
        raise Refuse("malformed commit pull association")
    for item in associated:
        if not isinstance(item, Mapping):
            continue
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        # Number lookup only. The associated PR's .head.sha is the live
        # current head, not a snapshot of the revision this run tested.
        candidates.setdefault(number, None)
    if not candidates:
        raise Refuse("missing trusted PR association")
    return candidates


def _immutable_source_heads(workflow_run: Mapping[str, Any], source_sha: str) -> set[str]:
    """SHAs the triggering run/event actually recorded.

    Live GET /pulls and /commits/{sha}/pulls heads are excluded: they describe
    the PR now, not the revision the run tested.
    """
    heads = {source_sha}
    event_prs = workflow_run.get("pull_requests") or []
    if not isinstance(event_prs, list):
        return heads
    for item in event_prs:
        if not isinstance(item, Mapping):
            continue
        head_obj = item.get("head")
        if not isinstance(head_obj, Mapping):
            continue
        head = _sha(head_obj.get("sha"))
        if head:
            heads.add(head)
    return heads


def associate_pull_request(
    workflow_run: Mapping[str, Any],
    repository: str,
    source_sha: str,
    api: GitHubAPI,
) -> tuple[int, str]:
    candidates = _candidate_numbers(workflow_run, repository, source_sha, api)
    trusted_heads = _immutable_source_heads(workflow_run, source_sha)
    matching: list[tuple[int, str]] = []
    for number, event_head in candidates.items():
        try:
            pull = api.get(f"/repos/{repository}/pulls/{number}")
        except GitHubError:
            continue
        if not isinstance(pull, Mapping):
            continue
        if pull.get("state") != "open":
            continue
        base_repo = _full_name((pull.get("base") or {}).get("repo") if isinstance(pull.get("base"), Mapping) else None)
        if base_repo and base_repo != repository:
            continue
        current = _sha((pull.get("head") or {}).get("sha") if isinstance(pull.get("head"), Mapping) else None)
        if current is None:
            continue
        expected = event_head or source_sha
        if current != expected or current not in trusted_heads:
            continue
        matching.append((number, current))
    if not matching:
        raise Refuse("stale head")
    unique = {item[0] for item in matching}
    if len(unique) != 1:
        raise Refuse("ambiguous PR association")
    return matching[0]


def _is_bot_author(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    if not isinstance(user, Mapping):
        return False
    return user.get("login") == BOT_LOGIN and user.get("type") == "Bot"


def _owned_comment(comment: Mapping[str, Any], repository: str, pr: int) -> bool:
    if not _is_bot_author(comment):
        return False
    body = comment.get("body")
    if not isinstance(body, str) or not body:
        return False
    first = body.splitlines()[0].strip()
    if first != MARKER:
        return False
    binding = parse_binding(body)
    if binding is None:
        return True
    return binding.repo == repository and binding.pr == pr


def _current_head(api: GitHubAPI, repository: str, pr: int) -> str:
    pull = api.get(f"/repos/{repository}/pulls/{pr}")
    if not isinstance(pull, Mapping):
        raise Refuse("missing trusted PR association")
    if pull.get("state") != "open":
        raise Refuse("missing trusted PR association")
    current = _sha((pull.get("head") or {}).get("sha") if isinstance(pull.get("head"), Mapping) else None)
    if current is None:
        raise Refuse("missing source revision")
    return current


def publish(
    event: Mapping[str, Any],
    artifact_dir: Path,
    repository: str,
    api: GitHubAPI,
) -> Outcome:
    try:
        return _publish(event, artifact_dir, repository, api)
    except Refuse as exc:
        return Outcome(wrote=False, action="none", reason=str(exc))


def _publish(
    event: Mapping[str, Any],
    artifact_dir: Path,
    repository: str,
    api: GitHubAPI,
) -> Outcome:
    if not isinstance(repository, str) or "/" not in repository:
        raise Refuse("wrong repository")
    run = _trusted_run(event, repository)
    report = load_report(artifact_dir)
    pr, expected_head = associate_pull_request(run, repository, run["head_sha"], api)
    current = _current_head(api, repository, pr)
    if current != expected_head:
        raise Refuse("stale head")
    markdown = bind_published_markdown(
        render_markdown(report),
        run_id=run["id"],
        head_sha=expected_head,
        repository=repository,
        pr=pr,
    )
    comments = api.paginate(f"/repos/{repository}/issues/{pr}/comments?per_page=100")
    if not isinstance(comments, list):
        raise Refuse("malformed comment listing")
    owned = [item for item in comments if isinstance(item, Mapping) and _owned_comment(item, repository, pr)]
    owned.sort(key=lambda item: int(item.get("id") or 0))
    for item in owned:
        binding = parse_binding(str(item.get("body") or ""))
        if binding is None:
            continue
        if binding.head == current and binding.run > run["id"]:
            raise Refuse("older run must not overwrite a newer head report")
    current = _current_head(api, repository, pr)
    if current != expected_head:
        raise Refuse("stale head")
    body = {"body": markdown}
    if owned:
        comment_id = owned[0].get("id")
        if not isinstance(comment_id, int):
            raise Refuse("malformed comment listing")
        api.patch(f"/repos/{repository}/issues/comments/{comment_id}", body)
        return Outcome(True, "update", f"updated comment {comment_id}", comment_id, pr)
    created = api.post(f"/repos/{repository}/issues/{pr}/comments", body)
    comment_id = created.get("id") if isinstance(created, Mapping) else None
    if not isinstance(comment_id, int):
        comment_id = None
    return Outcome(True, "create", "posted a new comment", comment_id, pr)


class UrllibGitHub:
    """Live GitHub client used only by the privileged workflow entrypoint."""

    def __init__(self, token: str) -> None:
        if not token:
            raise Refuse("missing GITHUB_TOKEN")
        self._token = token

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else API_ROOT + path
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "vaws-knowledge-review-publisher",
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
                raise Refuse("malformed comment listing")
            items.extend(payload)
            current = _next_link(headers.get("Link") or headers.get("link"))
        return items

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        payload, _headers = self._request("POST", path, body)
        return payload

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        payload, _headers = self._request("PATCH", path, body)
        return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--event", help="event JSON path; default GITHUB_EVENT_PATH")
    parser.add_argument("--repo", help="owner/name; default GITHUB_REPOSITORY")
    args = parser.parse_args(argv)

    event_path = args.event or os.environ.get("GITHUB_EVENT_PATH")
    repository = args.repo or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not event_path or not repository:
        print("bot/publish_comment: missing GITHUB_EVENT_PATH or GITHUB_REPOSITORY", file=sys.stderr)
        return 2
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"bot/publish_comment: cannot read event: {exc}", file=sys.stderr)
        return 2
    if not token:
        print("bot/publish_comment: missing GITHUB_TOKEN", file=sys.stderr)
        return 2
    outcome = publish(event, Path(args.artifact_dir), repository, UrllibGitHub(token))
    print(f"{outcome.action}: {outcome.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
