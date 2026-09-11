"""Trusted review-comment binding, including retained original-shell defects.

The original privileged shell from 9cf5f779 is kept as a red control: it
takes the artifact pull-request number as the write target, never checks the
current head, and treats a marker in any comment as ownership.

The replacement helper is exercised against the same cases plus negative
controls for wrong repository/workflow/event/run/artifact, malformed reports
and missing association. GitHub is an injected stub; nothing here publishes.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent
from vaws_knowledge.bot.publish_comment import (  # noqa: E402
    BOT_LOGIN,
    EXPECTED_WORKFLOW_NAME,
    FAIL_PERMITS,
    GitHubError,
    PASS_PERMITS,
    _next_link,
    bind_published_markdown,
    parse_binding,
    publish,
)
from vaws_knowledge.bot.report import MARKER, render_markdown  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "bot"
ORIGINAL_SHELL = FIXTURES / "original-comment-step.sh"
INERT_GH = FIXTURES / "inert_gh.py"
VALID_REPORT = FIXTURES / "publish" / "valid-report.json"
FAIL_REPORT = FIXTURES / "publish" / "fail-report.json"
SPOOFED_MD = FIXTURES / "publish" / "spoofed-pass.md"

OWNER_REPO = "vllm-ascend-workspace/vaws-knowledge"
SOURCE_SHA = "9cf5f779f82be08cdb7c4d0eb41634c6dfe3cb27"
NEW_SHA = "5051b11597766d90284946b57a3246adede35527"
RUN_ID = 34110613264


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_event(**workflow_updates: Any) -> dict[str, Any]:
    workflow_run: dict[str, Any] = {
        "id": RUN_ID,
        "name": EXPECTED_WORKFLOW_NAME,
        "path": ".github/workflows/pr-review.yml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SOURCE_SHA,
        "head_branch": "feat/review-bot-pipeline",
        "html_url": f"https://github.com/{OWNER_REPO}/actions/runs/{RUN_ID}",
        "repository": {"full_name": OWNER_REPO},
        "head_repository": {"full_name": OWNER_REPO},
        "pull_requests": [
            {
                "number": 5,
                "head": {"sha": SOURCE_SHA, "repo": {"full_name": OWNER_REPO}},
                "base": {"ref": "main", "repo": {"full_name": OWNER_REPO}},
            }
        ],
    }
    workflow_run.update(workflow_updates)
    return {"action": "completed", "workflow_run": workflow_run}


def make_pull(number: int = 5, *, sha: str = SOURCE_SHA, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "head": {"sha": sha, "repo": {"full_name": OWNER_REPO}},
        "base": {"ref": "main", "repo": {"full_name": OWNER_REPO}},
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Optional[dict[str, Any]]]] = []
        self.pulls: dict[int, dict[str, Any]] = {5: make_pull()}
        self.commit_pulls: dict[str, list[dict[str, Any]]] = {}
        self.comments: list[dict[str, Any]] = []
        self.post_id = 1001
        self.errors: dict[str, GitHubError] = {}

    def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        if path in self.errors:
            raise self.errors[path]
        if "/commits/" in path and path.endswith("/pulls"):
            sha = path.split("/commits/")[1].split("/")[0]
            return [dict(item) for item in self.commit_pulls.get(sha, [])]
        if "/pulls/" in path:
            number = int(path.rsplit("/", 1)[-1].split("?")[0])
            if number not in self.pulls:
                raise GitHubError(404, path, "not found")
            return dict(self.pulls[number])
        raise AssertionError(f"unexpected GET {path}")

    def paginate(self, path: str) -> list[Any]:
        self.calls.append(("GET-PAGINATE", path, None))
        return [dict(item) for item in self.comments]

    def post(self, path: str, body: dict[str, Any]) -> Any:
        self.calls.append(("POST", path, dict(body)))
        return {"id": self.post_id}

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        self.calls.append(("PATCH", path, dict(body)))
        return {"id": int(path.rsplit("/", 1)[-1])}

    def writes(self) -> list[tuple[str, str, Optional[dict[str, Any]]]]:
        return [item for item in self.calls if item[0] in {"POST", "PATCH"}]


def bot_comment(comment_id: int, body: str) -> dict[str, Any]:
    return {
        "id": comment_id,
        "user": {"login": BOT_LOGIN, "type": "Bot"},
        "body": body,
    }


def human_comment(comment_id: int, body: str) -> dict[str, Any]:
    return {
        "id": comment_id,
        "user": {"login": "alice", "type": "User"},
        "body": body,
    }


def bound_body(run_id: int = RUN_ID, head: str = SOURCE_SHA, pr: int = 5) -> str:
    report = _load_json(VALID_REPORT)
    return bind_published_markdown(
        render_markdown(report),
        run_id=run_id,
        head_sha=head,
        repository=OWNER_REPO,
        pr=pr,
    )


@unittest.skipIf(os.name == "nt", "historical GitHub Actions bash fixture; current publisher is tested below")
class OriginalShellRedControls(unittest.TestCase):
    """The exact 9cf5f779 comment shell, against an inert local gh."""

    def _run(
        self,
        *,
        pr_number: str,
        comments: list[dict[str, Any]],
    ) -> list[list[str]]:
        work = pathlib.Path(tempfile.mkdtemp())
        bin_dir = work / "bin"
        bin_dir.mkdir()
        gh_path = bin_dir / "gh"
        shutil.copy(INERT_GH, gh_path)
        gh_path.chmod(0o755)
        artifact = work / "artifact"
        artifact.mkdir()
        (artifact / "pr-number.txt").write_text(pr_number + "\n", encoding="utf-8")
        shutil.copy(SPOOFED_MD, artifact / "gate-comment.md")
        log_path = work / "gh-calls.jsonl"
        comments_path = work / "comments.json"
        comments_path.write_text(json.dumps(comments), encoding="utf-8")
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["REPO"] = OWNER_REPO
        env["GH_CALLS_LOG"] = str(log_path)
        env["GH_COMMENTS_PATH"] = str(comments_path)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        result = subprocess.run(
            ["bash", str(ORIGINAL_SHELL)],
            cwd=artifact,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    def test_original_shell_posts_to_the_artifact_pr_number(self):
        calls = self._run(pr_number="999", comments=[])
        joined = [" ".join(args) for args in calls]
        self.assertTrue(any("issues/999/comments" in item and "-X POST" in item for item in joined), calls)
        self.assertFalse(any("issues/5/comments" in item and "-X POST" in item for item in joined), calls)

    def test_original_shell_patches_without_checking_current_head(self):
        comments = [bot_comment(777, MARKER + "\nold")]
        calls = self._run(pr_number="5", comments=comments)
        joined = [" ".join(args) for args in calls]
        self.assertTrue(any("issues/comments/777" in item and "-X PATCH" in item for item in joined), calls)
        self.assertFalse(any("/pulls/5" in item for item in joined), calls)
        self.assertFalse(any("head" in item and "api" in item and "pulls" in item for item in joined), calls)

    def test_original_shell_treats_a_human_marker_as_ownership(self):
        comments = [
            human_comment(111, MARKER + "\nspoofed by a person"),
            bot_comment(888, MARKER + "\nreal bot comment"),
        ]
        calls = self._run(pr_number="5", comments=comments)
        joined = [" ".join(args) for args in calls]
        self.assertTrue(any("issues/comments/111" in item and "-X PATCH" in item for item in joined), calls)
        self.assertFalse(any("issues/comments/888" in item for item in joined), calls)


class PublisherHelper(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.work = pathlib.Path(tempfile.mkdtemp())
        shutil.copy(VALID_REPORT, self.work / "gate-results.json")
        shutil.copy(SPOOFED_MD, self.work / "gate-comment.md")
        (self.work / "pr-number.txt").write_text("999\n", encoding="utf-8")
        (self.work / "run-id.txt").write_text("1\n", encoding="utf-8")

    def _publish(self, event: Optional[dict[str, Any]] = None):
        return publish(event or make_event(), self.work, OWNER_REPO, self.api)

    def test_wrong_artifact_pr_number_is_not_the_write_target(self):
        outcome = self._publish()
        self.assertTrue(outcome.wrote, outcome.reason)
        self.assertEqual("create", outcome.action)
        writes = self.api.writes()
        self.assertEqual(1, len(writes))
        self.assertEqual("POST", writes[0][0])
        self.assertEqual(f"/repos/{OWNER_REPO}/issues/5/comments", writes[0][1])
        self.assertNotIn("999", writes[0][1])
        body = writes[0][2]["body"]
        self.assertNotIn("Synthetic untrusted artifact claiming PASS", body)
        self.assertIn(SOURCE_SHA, body)
        self.assertIn(str(RUN_ID), body)
        self.assertIn("corpus/unverified/", body)
        self.assertNotIn("corpus/verified/", body.split("corpus/unverified/")[0])

    def test_stale_head_writes_nothing(self):
        self.api.pulls[5] = make_pull(sha=NEW_SHA)
        outcome = self._publish()
        self.assertFalse(outcome.wrote, outcome.reason)
        self.assertEqual("stale head", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_human_marker_is_not_updated(self):
        spoofed = MARKER + "\nspoofed by a person"
        self.api.comments = [
            human_comment(111, spoofed),
            bot_comment(888, bound_body()),
        ]
        outcome = self._publish()
        self.assertTrue(outcome.wrote, outcome.reason)
        writes = self.api.writes()
        self.assertEqual(1, len(writes))
        self.assertEqual("PATCH", writes[0][0])
        self.assertEqual(f"/repos/{OWNER_REPO}/issues/comments/888", writes[0][1])
        self.assertNotIn("111", writes[0][1])

    def test_human_only_marker_creates_a_bot_comment(self):
        self.api.comments = [human_comment(111, MARKER + "\nspoofed by a person")]
        outcome = self._publish()
        self.assertEqual("create", outcome.action)
        writes = self.api.writes()
        self.assertEqual("POST", writes[0][0])
        self.assertEqual(f"/repos/{OWNER_REPO}/issues/5/comments", writes[0][1])
        self.assertNotIn("comments/111", writes[0][1])

    def test_pagination_skips_an_unrelated_first_marker(self):
        self.api.comments = [
            human_comment(1, MARKER + "\nfirst match would be wrong"),
            {
                "id": 2,
                "user": {"login": "dependabot[bot]", "type": "Bot"},
                "body": MARKER + "\nwrong bot",
            },
            bot_comment(3, bound_body()),
        ]
        outcome = self._publish()
        self.assertTrue(outcome.wrote, outcome.reason)
        writes = self.api.writes()
        self.assertEqual([("PATCH", f"/repos/{OWNER_REPO}/issues/comments/3", writes[0][2])], writes)

    def test_valid_same_head_creates_a_comment(self):
        outcome = self._publish()
        self.assertEqual("create", outcome.action)
        body = self.api.writes()[0][2]["body"]
        binding = parse_binding(body)
        self.assertIsNotNone(binding)
        self.assertEqual(RUN_ID, binding.run)
        self.assertEqual(SOURCE_SHA, binding.head)
        self.assertEqual(OWNER_REPO, binding.repo)
        self.assertEqual(5, binding.pr)
        self.assertEqual(MARKER, body.splitlines()[0])
        self.assertIn("Documents load", body)

    def test_valid_same_head_updates_the_bot_comment(self):
        self.api.comments = [bot_comment(777, bound_body())]
        outcome = self._publish()
        self.assertEqual("update", outcome.action)
        self.assertEqual(777, outcome.comment_id)
        self.assertEqual("PATCH", self.api.writes()[0][0])
        self.assertEqual(f"/repos/{OWNER_REPO}/issues/comments/777", self.api.writes()[0][1])

    def test_legacy_bot_marker_without_binding_is_updated(self):
        self.api.comments = [bot_comment(777, MARKER + "\nlegacy body")]
        outcome = self._publish()
        self.assertEqual("update", outcome.action)
        self.assertIn(SOURCE_SHA, self.api.writes()[0][2]["body"])

    def test_old_run_does_not_overwrite_a_newer_head_report(self):
        self.api.comments = [bot_comment(777, bound_body(run_id=RUN_ID + 1))]
        outcome = self._publish()
        self.assertFalse(outcome.wrote, outcome.reason)
        self.assertEqual("older run must not overwrite a newer head report", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_wrong_repository_writes_nothing(self):
        event = make_event(repository={"full_name": "other/repo"})
        outcome = self._publish(event)
        self.assertFalse(outcome.wrote)
        self.assertEqual("wrong repository", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_wrong_workflow_writes_nothing(self):
        outcome = self._publish(make_event(name="Review"))
        self.assertFalse(outcome.wrote)
        self.assertEqual("wrong workflow", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_unrelated_push_event_writes_nothing(self):
        outcome = self._publish(make_event(event="push"))
        self.assertFalse(outcome.wrote)
        self.assertEqual("unrelated event", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_artifact_run_id_file_is_not_authority(self):
        outcome = self._publish()
        self.assertTrue(outcome.wrote, outcome.reason)
        binding = parse_binding(self.api.writes()[0][2]["body"])
        self.assertEqual(RUN_ID, binding.run)
        self.assertNotEqual(1, binding.run)

    def test_missing_report_writes_nothing(self):
        (self.work / "gate-results.json").unlink()
        outcome = self._publish()
        self.assertFalse(outcome.wrote)
        self.assertEqual("missing report", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_malformed_report_writes_nothing(self):
        (self.work / "gate-results.json").write_text("{", encoding="utf-8")
        outcome = self._publish()
        self.assertFalse(outcome.wrote)
        self.assertEqual("malformed report", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_inconsistent_pass_cannot_generate_a_success_comment(self):
        report = _load_json(VALID_REPORT)
        report["overall"] = "pass"
        report["gates"][0]["status"] = "fail"
        report["counts"]["failed"] = 1
        report["counts"]["passed"] = 0
        (self.work / "gate-results.json").write_text(json.dumps(report), encoding="utf-8")
        outcome = self._publish()
        self.assertFalse(outcome.wrote)
        self.assertIn("success comment", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_pass_with_verified_permits_cannot_generate_a_success_comment(self):
        report = _load_json(VALID_REPORT)
        report["permits"] = "corpus/verified/"
        (self.work / "gate-results.json").write_text(json.dumps(report), encoding="utf-8")
        outcome = self._publish()
        self.assertFalse(outcome.wrote)
        self.assertIn("success comment", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_markdown_only_artifact_writes_nothing(self):
        (self.work / "gate-results.json").unlink()
        shutil.copy(SPOOFED_MD, self.work / "gate-results.json")
        outcome = self._publish()
        self.assertFalse(outcome.wrote)
        self.assertEqual("malformed report", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_missing_trusted_association_writes_nothing(self):
        event = make_event(pull_requests=[])
        outcome = self._publish(event)
        self.assertFalse(outcome.wrote)
        self.assertEqual("missing trusted PR association", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_associates_the_pr_from_the_commit_api_when_the_event_list_is_empty(self):
        self.api.commit_pulls[SOURCE_SHA] = [make_pull()]
        outcome = self._publish(make_event(pull_requests=[]))
        self.assertTrue(outcome.wrote, outcome.reason)
        self.assertEqual("create", outcome.action)
        writes = self.api.writes()
        self.assertEqual(1, len(writes))
        self.assertEqual("POST", writes[0][0])
        self.assertIn(f"/repos/{OWNER_REPO}/issues/5/comments", writes[0][1])
        binding = parse_binding(writes[0][2]["body"])
        self.assertIsNotNone(binding)
        self.assertEqual(SOURCE_SHA, binding.head)
        self.assertNotEqual(NEW_SHA, binding.head)

    def test_commit_association_must_not_launder_a_newer_head(self):
        # Empty event PR list, run head A, associated/current open PR head B.
        # The association endpoint identifies the PR; its live head is not
        # evidence that run A tested B.
        self.api.commit_pulls[SOURCE_SHA] = [make_pull(sha=NEW_SHA)]
        self.api.pulls[5] = make_pull(sha=NEW_SHA)
        outcome = self._publish(make_event(pull_requests=[]))
        self.assertFalse(outcome.wrote, outcome.reason)
        self.assertEqual("stale head", outcome.reason)
        self.assertEqual([], self.api.writes())

    def test_same_head_fallback_ignores_live_association_head_field(self):
        self.api.commit_pulls[SOURCE_SHA] = [make_pull(sha=NEW_SHA)]
        self.api.pulls[5] = make_pull(sha=SOURCE_SHA)
        outcome = self._publish(make_event(pull_requests=[]))
        self.assertTrue(outcome.wrote, outcome.reason)
        binding = parse_binding(self.api.writes()[0][2]["body"])
        self.assertEqual(SOURCE_SHA, binding.head)
        self.assertNotIn(NEW_SHA, self.api.writes()[0][2]["body"])

    def test_valid_fail_report_still_publishes_a_failure_comment(self):
        shutil.copy(FAIL_REPORT, self.work / "gate-results.json")
        outcome = self._publish()
        self.assertTrue(outcome.wrote, outcome.reason)
        body = self.api.writes()[0][2]["body"]
        self.assertIn("**Result: FAIL**", body)
        self.assertNotIn("**Result: PASS**", body)

    def test_spoofed_markdown_is_not_the_published_body(self):
        outcome = self._publish()
        body = self.api.writes()[0][2]["body"]
        self.assertEqual(render_markdown(_load_json(VALID_REPORT)).splitlines()[0], MARKER)
        self.assertIn("vaws-knowledge review bot", body)
        self.assertNotEqual(SPOOFED_MD.read_text(encoding="utf-8"), body)

    def test_pass_permits_constant_is_the_gates_string(self):
        import vaws_knowledge.bot.gates as gates_mod

        text = pathlib.Path(gates_mod.__file__).read_text(encoding="utf-8")
        self.assertIn(PASS_PERMITS, text)
        self.assertIn(FAIL_PERMITS, text)
        self.assertIn("unverified", PASS_PERMITS)
        self.assertNotIn("corpus/verified/", PASS_PERMITS)

    def test_next_link_ignores_unrelated_hosts(self):
        self.assertIsNone(_next_link('<https://example.invalid/items?page=2>; rel="next"'))
        self.assertEqual(
            "/repos/x/y/issues/5/comments?page=2",
            _next_link(
                '<https://api.github.com/repos/x/y/issues/5/comments?page=2>; rel="next"'
            ),
        )


if __name__ == "__main__":
    unittest.main()
