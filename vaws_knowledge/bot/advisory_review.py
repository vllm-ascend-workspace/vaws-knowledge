"""Trusted advisory review for incoming knowledge PR data.

Runs on the default-branch workflow. The secret-bearing path never checks out
pull-request code. It independently fetches selected corpus YAML at the
authenticated event's immutable source head, re-runs trusted mandatory gates
via bot/triage_grok.py, and may publish a *separate* advisory comment.

A current mutable PR head cannot enlarge an old run's immutable head set
(bot.publish_comment.associate_pull_request). Missing XAI_API_KEY / XAI_MODEL
is a visible unavailable result with zero provider calls.

Usage::

    python3 bot/advisory_review.py --stash /tmp/vaws-advisory --json advisory.json
    python3 bot/advisory_review.py --publish-comment --artifact-dir advisory-report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from vaws_knowledge.bot.publish_comment import (
    BOT_LOGIN,
    EXPECTED_EVENT,
    EXPECTED_WORKFLOW_NAME,
    EXPECTED_WORKFLOW_PATH,
    GitHubAPI,
    GitHubError,
    Refuse,
    UrllibGitHub,
    _current_head,
    _full_name,
    _sha,
    associate_pull_request,
)
from vaws_knowledge.bot.report import MARKER
from vaws_knowledge.bot.triage_grok import Transport, run_advisory
from vaws_knowledge.sync import collect as collect_mod

ADVISORY_WORKFLOW_NAME = "Advisory review"
ADVISORY_WORKFLOW_PATH = ".github/workflows/advisory-review.yml"
ADVISORY_MARKER = "<!-- vaws-knowledge-advisory-grok:v1 -->"
ADVISORY_ARTIFACT_NAME = "advisory-review"
KIND = "advisory-review-wiring"

MAX_ARTIFACT_BYTES = 1_048_576


class AdvisoryRefuse(Refuse):
    """Fail closed: no model call enlargement and no comment write."""


@dataclass
class CorpusFetch:
    files: list[Path] = field(default_factory=list)
    selected: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


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
        "head_repository": _full_name(workflow_run.get("head_repository")) or repository,
        "raw": workflow_run,
    }


def fetch_selected_corpus(
    api: GitHubAPI,
    repository: str,
    head_sha: str,
    stash: Path,
    *,
    bounds: Optional[collect_mod.Bounds] = None,
) -> CorpusFetch:
    """Fetch corpus/ and examples/ YAML blobs at an immutable head into stash."""
    bounds = bounds or collect_mod.Bounds()
    stash = stash.resolve()
    stash.mkdir(parents=True, exist_ok=True)
    collect_mod.ensure_stash_not_on_path(stash)
    result = CorpusFetch()
    try:
        entries, truncated = collect_mod.list_tree_entries(api, repository, head_sha)
    except GitHubError as exc:
        raise AdvisoryRefuse(f"tree fetch failed ({exc.status})") from exc
    except collect_mod.CollectRefuse as exc:
        raise AdvisoryRefuse(str(exc)) from exc
    result.truncated = bool(truncated)
    selected_entries: list[Mapping[str, Any]] = []
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        reason = collect_mod.validate_selected_corpus_path(path)
        if reason is not None:
            continue
        if entry.get("type") != "blob" or entry.get("mode") != collect_mod.ORDINARY_BLOB_MODE:
            continue
        blob_sha = _sha(entry.get("sha"))
        if blob_sha is None:
            result.failed.append({"path": path, "reason": "missing_blob_sha"})
            continue
        selected_entries.append(entry)
    if truncated:
        result.omitted.append({"reason": "truncated_tree", "path": None})
    total = 0
    for entry in selected_entries:
        path = str(entry.get("path"))
        blob_sha = _sha(entry.get("sha"))
        pointer = {"path": path, "blob_sha": blob_sha, "commit_sha": head_sha}
        size = entry.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size > bounds.max_file_bytes:
            result.omitted.append({**pointer, "reason": "oversize"})
            continue
        if len(result.files) >= bounds.max_files_per_repo or total >= bounds.max_total_bytes:
            result.omitted.append({**pointer, "reason": "fetch_bound"})
            continue
        try:
            data = collect_mod.fetch_blob(api, repository, blob_sha, max_bytes=bounds.max_file_bytes)
        except (GitHubError, collect_mod.CollectRefuse) as exc:
            reason = "blob_fetch_failed"
            if isinstance(exc, collect_mod.CollectRefuse):
                reason = str(exc) or reason
            result.failed.append({**pointer, "reason": reason})
            continue
        dest = collect_mod._stash_file(stash, 0, head_sha, path, data)
        result.files.append(dest)
        result.selected.append({**pointer, "status": "fetched"})
        total += len(data)
    return result


def run_trusted_advisory(
    event: Mapping[str, Any],
    repository: str,
    api: GitHubAPI,
    stash: Path,
    *,
    root: Path,
    environ: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
    python: str = sys.executable,
    fetch_corpus: bool = True,
) -> dict[str, Any]:
    """Associate the PR, fetch immutable corpus, run the accepted adapter."""
    run = _trusted_run(event, repository)
    pr, expected_head = associate_pull_request(run["raw"], repository, run["head_sha"], api)
    current = _current_head(api, repository, pr)
    if current != expected_head:
        raise Refuse("stale head")

    paths: list[str] = []
    fetch_error: Optional[str] = None
    fetched = CorpusFetch()
    if fetch_corpus:
        try:
            fetched = fetch_selected_corpus(api, repository, run["head_sha"], stash)
            paths = [str(path) for path in fetched.files]
        except AdvisoryRefuse as exc:
            fetch_error = str(exc)

    fetch_omitted = list(fetched.omitted) + [
        {**item, "reason": item.get("reason") or "blob_fetch_failed"} for item in fetched.failed
    ]
    artifact: dict[str, Any]
    fail_closed_fetch = bool(fetched.failed) or bool(fetch_error)
    if fail_closed_fetch or not paths:
        reason = fetch_error
        if fetched.failed:
            reason = "selected corpus blob fetch failed"
        elif not paths:
            reason = fetch_error or "no eligible input"
        artifact = {
            "advisory": True,
            "kind": KIND,
            "status": "unavailable",
            "reason": reason,
            "provider": {"called": False},
            "notes": list(_advisory_notes()),
            "input_gates": [],
            "coverage": {
                "selected": [],
                "selected_count": 0,
                "omitted": fetch_omitted,
                "omitted_count": len(fetch_omitted),
            },
            "source": {
                "repo": repository,
                "ref": run["head_sha"],
                "paths": [],
            },
        }
    else:
        artifact = run_advisory(
            paths,
            source_repo=repository,
            source_ref=run["head_sha"],
            root=root,
            environ=environ if environ is not None else os.environ,
            transport=transport,
            python=python,
        )
        coverage = artifact.setdefault("coverage", {})
        omitted = list(coverage.get("omitted") or []) + fetch_omitted
        coverage["omitted"] = omitted
        coverage["omitted_count"] = len(omitted)
    artifact["fetch"] = {
        "selected": fetched.selected,
        "failed": fetched.failed,
        "omitted": fetched.omitted,
        "truncated": fetched.truncated,
    }
    artifact["binding"] = {
        "run": run["id"],
        "head": run["head_sha"],
        "repo": repository,
        "pr": pr,
        "immutable_heads": [run["head_sha"]],
    }
    artifact["workflow"] = {
        "listened": EXPECTED_WORKFLOW_NAME,
        "publisher": ADVISORY_WORKFLOW_NAME,
        "path": ADVISORY_WORKFLOW_PATH,
    }
    artifact.setdefault("notes", [])
    artifact["notes"] = list(dict.fromkeys(list(artifact.get("notes") or []) + list(_advisory_notes())))
    if artifact.get("status") == "success":
        artifact["permits"] = "nothing; advisory only"
    elif artifact.get("status") == "unavailable":
        artifact["permits"] = "nothing; advisory unavailable is not a successful semantic review"
    else:
        artifact["permits"] = "nothing"
    return artifact


def _advisory_notes() -> tuple[str, ...]:
    return (
        "This section is advisory Grok triage. It is not a gate verdict.",
        "Model output cannot approve a change, set status=verified, advance "
        "re-verification dates, resolve conflicts, or edit source.",
        "Promotion remains behind evidence and non-submitter confirmation.",
        "Unavailable configuration is not an empty successful semantic review.",
    )


def render_advisory_markdown(artifact: Mapping[str, Any]) -> str:
    binding = artifact.get("binding") if isinstance(artifact.get("binding"), Mapping) else {}
    status = str(artifact.get("status") or "unavailable")
    reason = artifact.get("reason")
    lines = [
        ADVISORY_MARKER,
        f"<!-- vaws-knowledge-advisory-binding run={binding.get('run')} "
        f"head={binding.get('head')} repo={binding.get('repo')} pr={binding.get('pr')} -->",
        "## vaws-knowledge advisory Grok triage",
        "",
        f"**Advisory status: {status}.** This is not the deterministic review verdict.",
        "",
    ]
    notes = list(artifact.get("notes") or [])
    for note in _advisory_notes():
        if note not in notes:
            notes.append(note)
    for note in notes:
        lines.append(f"- {note}")
    lines.append("")
    if reason:
        lines.append(f"Reason: {reason}")
        lines.append("")
    if status == "success":
        candidates = artifact.get("candidates") or []
        if candidates:
            lines.append("Suggested contradiction pairs (advisory only):")
            for pair in candidates:
                if not isinstance(pair, Mapping):
                    continue
                lines.append(
                    f"- `{pair.get('a')}` ↔ `{pair.get('b')}`"
                )
        else:
            lines.append("No contradiction candidates (successful empty advisory result).")
        lines.append("")
    provider = artifact.get("provider") if isinstance(artifact.get("provider"), Mapping) else {}
    lines.append(f"Provider called: {'yes' if provider.get('called') else 'no'}.")
    lines.append("")
    lines.append(
        f"_Bound to run {binding.get('run')} at `{binding.get('head')}` "
        f"(PR #{binding.get('pr')})._"
    )
    return "\n".join(lines) + "\n"


def _is_bot_author(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    if not isinstance(user, Mapping):
        return False
    return user.get("login") == BOT_LOGIN and user.get("type") == "Bot"


def _owned_advisory_comment(comment: Mapping[str, Any], repository: str, pr: int) -> bool:
    if not _is_bot_author(comment):
        return False
    body = comment.get("body")
    if not isinstance(body, str) or not body:
        return False
    first = body.splitlines()[0].strip()
    if first != ADVISORY_MARKER:
        return False
    if MARKER in body.splitlines()[:3]:
        return False
    return True


def publish_advisory_comment(
    event: Mapping[str, Any],
    artifact: Mapping[str, Any],
    repository: str,
    api: GitHubAPI,
) -> dict[str, Any]:
    run = _trusted_run(event, repository)
    pr, expected_head = associate_pull_request(run["raw"], repository, run["head_sha"], api)
    current = _current_head(api, repository, pr)
    if current != expected_head:
        raise Refuse("stale head")
    binding = artifact.get("binding")
    if not isinstance(binding, Mapping):
        raise Refuse("missing advisory binding")
    bound_run = binding.get("run")
    bound_head = _sha(binding.get("head"))
    bound_repo = binding.get("repo")
    bound_pr = binding.get("pr")
    if (
        bound_run != run["id"]
        or bound_head != run["head_sha"]
        or bound_repo != repository
        or bound_pr != pr
        or bound_head != expected_head
        or bound_head != current
    ):
        raise Refuse("mismatched advisory binding")
    markdown = render_advisory_markdown(artifact)
    if not markdown.startswith(ADVISORY_MARKER):
        raise Refuse("advisory marker missing")
    if markdown.startswith(MARKER):
        raise Refuse("advisory comment must not use the deterministic marker")
    comments = api.paginate(f"/repos/{repository}/issues/{pr}/comments?per_page=100")
    if not isinstance(comments, list):
        raise Refuse("malformed comment listing")
    owned = [
        item
        for item in comments
        if isinstance(item, Mapping) and _owned_advisory_comment(item, repository, pr)
    ]
    current = _current_head(api, repository, pr)
    if current != expected_head:
        raise Refuse("stale head")
    body = {"body": markdown}
    if owned:
        comment_id = owned[0].get("id")
        if not isinstance(comment_id, int):
            raise Refuse("malformed comment listing")
        api.patch(f"/repos/{repository}/issues/comments/{comment_id}", body)
        return {"wrote": True, "action": "update", "comment_id": comment_id, "pr": pr}
    created = api.post(f"/repos/{repository}/issues/{pr}/comments", body)
    comment_id = created.get("id") if isinstance(created, Mapping) else None
    return {"wrote": True, "action": "create", "comment_id": comment_id, "pr": pr}


def load_advisory_artifact(artifact_dir: Path) -> dict[str, Any]:
    path = artifact_dir / "advisory.json"
    if not path.is_file():
        raise Refuse("missing advisory artifact")
    data = path.read_bytes()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise Refuse("malformed advisory artifact")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refuse("malformed advisory artifact") from exc
    if not isinstance(payload, dict) or payload.get("advisory") is not True:
        raise Refuse("malformed advisory artifact")
    if payload.get("status") == "success" and payload.get("permits") not in {
        "nothing; advisory only",
        "nothing",
    }:
        raise Refuse("advisory artifact must not claim a gate permit")
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stash", default=None)
    parser.add_argument("--json", default=None)
    parser.add_argument("--artifact-dir", default=None)
    parser.add_argument("--publish-comment", action="store_true")
    parser.add_argument("--event", help="event JSON path; default GITHUB_EVENT_PATH")
    parser.add_argument("--repo", help="owner/name; default GITHUB_REPOSITORY")
    args = parser.parse_args(argv)

    event_path = args.event or os.environ.get("GITHUB_EVENT_PATH")
    repository = args.repo or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not event_path or not repository:
        print("bot/advisory_review: missing GITHUB_EVENT_PATH or GITHUB_REPOSITORY", file=sys.stderr)
        return 2
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"bot/advisory_review: cannot read event: {exc}", file=sys.stderr)
        return 2

    if args.publish_comment:
        if not args.artifact_dir:
            print("bot/advisory_review: --artifact-dir is required to publish", file=sys.stderr)
            return 2
        if not token:
            print("bot/advisory_review: missing GITHUB_TOKEN", file=sys.stderr)
            return 2
        try:
            artifact = load_advisory_artifact(Path(args.artifact_dir))
            outcome = publish_advisory_comment(event, artifact, repository, UrllibGitHub(token))
        except Refuse as exc:
            print(f"none: {exc}")
            return 0
        print(f"{outcome['action']}: advisory comment")
        return 0

    if not token:
        print("bot/advisory_review: missing GITHUB_TOKEN", file=sys.stderr)
        return 2
    stash = Path(args.stash or os.environ.get("RUNNER_TEMP") or "/tmp") / "vaws-advisory"
    try:
        artifact = run_trusted_advisory(
            event,
            repository,
            UrllibGitHub(token),
            stash,
            root=Path(__file__).resolve().parent.parent,
        )
    except Refuse as exc:
        print(f"none: {exc}")
        artifact = {
            "advisory": True,
            "status": "unavailable",
            "reason": str(exc),
            "provider": {"called": False},
            "notes": list(_advisory_notes()),
            "permits": "nothing; advisory unavailable is not a successful semantic review",
        }
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        json.dump(artifact, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
