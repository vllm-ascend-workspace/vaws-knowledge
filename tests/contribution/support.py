"""Fakes and local git helpers for contribution tests. No live network."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from vaws_knowledge.bot.publish_comment import GitHubError
from vaws_knowledge.bot.triage_grok import HttpRequest, HttpResponse, TransportFailure
from vaws_knowledge.contribution.ci import encode_blob
from vaws_knowledge.contribution.documents import ContentIdentity, MarkdownDocument
from vaws_knowledge.contribution.grok import Classification
from vaws_knowledge.contribution.recall import RelatedDocument

OWNER_REPO = "owner/vaws-knowledge-corpus"
DEFAULT_BRANCH = "main"


def git_sha(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()


HEAD_A = git_sha("head-a")
HEAD_B = git_sha("head-b")
BASE_1 = git_sha("base-1")
BASE_2 = git_sha("base-2")
RELATED_SHA = git_sha("related-doc")


def ordinary_md(title: str, body: str) -> str:
    return MarkdownDocument.from_text(f"# {title}\n\n{body}\n").render()


def related_doc(path: str, title: str, body: str, *, git_sha_value: str = RELATED_SHA) -> RelatedDocument:
    return RelatedDocument(
        identity=ContentIdentity(path=path, git_sha=git_sha_value),
        title=title,
        body=body,
        uri=f"viking://resources/{path}",
        score=0.9,
    )


def success_class(
    decision: str,
    *,
    reason: str = "fixture",
    strength: str = "ordinary_observation",
    commensurate: bool = True,
    related_ids: list[str] | None = None,
    scope: str = "",
    summary: str = "",
) -> Classification:
    return Classification(
        status="success",
        decision=decision,
        reason=reason,
        claim_strength=strength,
        related_ids=related_ids or [],
        scope_note=scope,
        evidence_commensurate=commensurate,
        supplement_summary=summary,
        provider_called=False,
    )


def init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", DEFAULT_BRANCH], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "contrib-test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Contribution Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# fixture corpus\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


class FakeTransport:
    def __init__(
        self,
        response: Optional[HttpResponse] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.calls: list[HttpRequest] = []
        self.response = response
        self.error = error

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise TransportFailure("fixture transport has no response")
        return self.response


class FakeContributionGitHub:
    def __init__(self, *, base_sha: str = BASE_1) -> None:
        self.calls: list[tuple[str, str, Optional[dict[str, Any]]]] = []
        self.refs: dict[str, str] = {DEFAULT_BRANCH: base_sha}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.next_pr = 1
        self.comments: list[dict[str, Any]] = []
        self.trees: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.permissions: dict[str, str] = {}
        self.errors: dict[str, GitHubError] = {}
        self.network_down = False
        self.auth_fail = False
        self.post_id = 500

    def add_pull(
        self,
        *,
        number: int | None = None,
        head: str,
        base: str | None = None,
        branch: str = "contrib/ab",
        title: str = "candidate",
        state: str = "open",
    ) -> dict[str, Any]:
        number = number or self.next_pr
        self.next_pr = max(self.next_pr, number + 1)
        pull = {
            "number": number,
            "state": state,
            "title": title,
            "html_url": f"https://github.com/{OWNER_REPO}/pull/{number}",
            "head": {
                "sha": head,
                "ref": branch,
                "repo": {"full_name": OWNER_REPO},
            },
            "base": {
                "sha": base or self.refs[DEFAULT_BRANCH],
                "ref": DEFAULT_BRANCH,
                "repo": {"full_name": OWNER_REPO},
            },
        }
        self.pulls[number] = pull
        self.commits[head] = {"sha": head, "tree": {"sha": head}, "parents": [], "message": "pr"}
        return pull

    def add_human_comment(
        self,
        *,
        login: str,
        body: str,
        in_reply_to: int | None = None,
        user_type: str = "User",
    ) -> dict[str, Any]:
        self.post_id += 1
        item: dict[str, Any] = {
            "id": self.post_id,
            "body": body,
            "user": {"login": login, "type": user_type},
        }
        if in_reply_to is not None:
            item["in_reply_to"] = in_reply_to
        self.comments.append(item)
        return item

    def add_markdown_tree(
        self,
        head: str,
        files: Mapping[str, bytes],
        *,
        truncated: bool = False,
    ) -> None:
        tree = []
        for path, data in files.items():
            blob = encode_blob(data)
            self.blobs[blob["sha"]] = data
            mode = "100644"
            if path.endswith(".py") or path.endswith(".sh"):
                mode = "100644"
            tree.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"], "size": len(data)})
        self.trees[head] = {"tree": tree, "truncated": truncated}

    def _fail(self, path: str) -> None:
        if self.network_down:
            raise GitHubError(0, path, "network down")
        if self.auth_fail:
            raise GitHubError(401, path, "unauthorized")
        if path in self.errors:
            raise self.errors[path]

    def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        self._fail(path)
        parsed = urlparse(path)
        bare = parsed.path
        if "/collaborators/" in bare and bare.endswith("/permission"):
            user = bare.rsplit("/collaborators/", 1)[-1].split("/")[0]
            perm = self.permissions.get(user)
            if not perm:
                raise GitHubError(404, path, "not a collaborator")
            return {"permission": perm, "user": {"login": user}}
        if "/git/commits/" in bare:
            sha = bare.rsplit("/", 1)[-1]
            if sha not in self.commits:
                raise GitHubError(404, path, "no commit")
            return dict(self.commits[sha])
        if "/git/ref/heads/" in bare:
            branch = bare.rsplit("/git/ref/heads/", 1)[-1]
            if branch not in self.refs:
                raise GitHubError(404, path, "missing ref")
            return {"object": {"sha": self.refs[branch], "type": "commit"}}
        if "/git/trees/" in bare:
            sha = bare.rsplit("/git/trees/", 1)[-1]
            if sha not in self.trees:
                return {"tree": [], "truncated": False}
            return dict(self.trees[sha])
        if "/git/blobs/" in bare:
            sha = bare.rsplit("/git/blobs/", 1)[-1]
            if sha not in self.blobs:
                raise GitHubError(404, path, "no blob")
            return encode_blob(self.blobs[sha])
        if "/pulls/" in bare and bare.rsplit("/", 1)[-1].isdigit():
            number = int(bare.rsplit("/", 1)[-1])
            if number not in self.pulls:
                raise GitHubError(404, path, "not found")
            return dict(self.pulls[number])
        if bare.endswith("/pulls"):
            query = parse_qs(parsed.query)
            head = (query.get("head") or [None])[0]
            state = (query.get("state") or ["open"])[0]
            found = []
            for pull in self.pulls.values():
                if state != "all" and pull.get("state") != state:
                    continue
                if head and pull.get("head", {}).get("ref") != head and f"owner:{pull.get('head', {}).get('ref')}" != head:
                    continue
                found.append(dict(pull))
            return found
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path: str) -> list[Any]:
        self.calls.append(("GET-PAGINATE", path, None))
        self._fail(path)
        return [dict(item) for item in self.comments]

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(("POST", path, dict(body)))
        self._fail(path)
        if path.endswith("/pulls"):
            head = str(body.get("head") or "contrib/x")
            branch = head.split(":", 1)[-1]
            for existing in self.pulls.values():
                if existing.get("head", {}).get("ref") == branch and existing.get("state") == "open":
                    return dict(existing)
            pull = self.add_pull(head=self.refs.get(branch) or git_sha(branch), branch=branch, title=str(body.get("title") or ""))
            return dict(pull)
        if path.endswith("/comments"):
            self.post_id += 1
            item = {
                "id": self.post_id,
                "body": body.get("body"),
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            }
            self.comments.append(item)
            return item
        if path.endswith("/git/blobs"):
            raw = str(body.get("content") or "").encode("utf-8")
            sha = git_sha("blob:" + raw.decode("utf-8", "replace"))
            self.blobs[sha] = raw
            return {"sha": sha}
        if path.endswith("/git/trees"):
            sha = git_sha("tree:" + json.dumps(body, sort_keys=True))
            return {"sha": sha, "tree": body.get("tree")}
        if path.endswith("/git/commits"):
            sha = git_sha("commit:" + json.dumps(body, sort_keys=True))
            self.commits[sha] = {
                "sha": sha,
                "tree": {"sha": body.get("tree")},
                "parents": list(body.get("parents") or []),
                "message": body.get("message"),
            }
            return {"sha": sha}
        return {"ok": True}

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(("PUT", path, dict(body)))
        self._fail(path)
        if path.endswith("/merge"):
            number = int(path.split("/pulls/")[1].split("/")[0])
            pull = self.pulls.get(number)
            if pull is None:
                raise GitHubError(404, path, "not found")
            if body.get("sha") != pull["head"]["sha"]:
                raise GitHubError(409, path, "Head branch was modified")
            base_ref = pull["base"]["ref"]
            if self.refs.get(base_ref) != pull["base"]["sha"]:
                raise GitHubError(409, path, "base moved")
            new_sha = git_sha(f"merge:{self.refs[base_ref]}:{pull['head']['sha']}")
            self.refs[base_ref] = new_sha
            pull["state"] = "closed"
            pull["merged"] = True
            return {"merged": True, "sha": new_sha}
        raise AssertionError(f"unexpected PUT {path}")

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(("PATCH", path, dict(body)))
        self._fail(path)
        if "/git/refs/heads/" in path:
            branch = path.rsplit("/git/refs/heads/", 1)[-1]
            sha = str(body.get("sha") or "")
            self.refs[branch] = sha
            for pull in self.pulls.values():
                if pull.get("head", {}).get("ref") == branch:
                    pull["head"]["sha"] = sha
            return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}
        if "/pulls/" in path:
            number = int(path.rsplit("/", 1)[-1])
            if number in self.pulls:
                updates = dict(body)
                if "head" in updates and isinstance(updates["head"], dict):
                    self.pulls[number]["head"].update(updates.pop("head"))
                self.pulls[number].update(updates)
                return dict(self.pulls[number])
        return dict(body)
