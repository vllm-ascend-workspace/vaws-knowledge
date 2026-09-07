"""Workflow name, permission and checkout boundaries for review publishing.

A YAML file that parses locally is not GitHub validity. These cases pin the
explicit split GitHub rejected at head 9cf5f779: the gate workflow must not
listen to itself, incoming PR code must stay read-only, and the publisher
must not check out or install pull-request code.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bot.publish_comment import (  # noqa: E402
    EXPECTED_ARTIFACT_NAME,
    EXPECTED_WORKFLOW_NAME,
    EXPECTED_WORKFLOW_PATH,
    PUBLISHER_WORKFLOW_NAME,
)

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required to read workflow fixtures: {exc}") from exc


GATES_PATH = REPO / ".github" / "workflows" / "pr-review.yml"
COMMENT_PATH = REPO / ".github" / "workflows" / "pr-review-comment.yml"


def _load(path: pathlib.Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    if not isinstance(data, dict):
        raise AssertionError(f"{path} did not load as a mapping")
    return text, data


def _on(data: dict) -> dict:
    trigger = data.get("on")
    if trigger is None:
        trigger = data.get(True)
    if not isinstance(trigger, dict):
        raise AssertionError("workflow is missing an `on` mapping")
    return trigger


def _job(data: dict, name: str) -> dict:
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or name not in jobs:
        raise AssertionError(f"missing job {name}")
    job = jobs[name]
    if not isinstance(job, dict):
        raise AssertionError(f"job {name} is not a mapping")
    return job


def _steps(job: dict) -> list:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise AssertionError("job has no steps")
    return steps


def _checkout(job: dict) -> dict:
    for step in _steps(job):
        if isinstance(step, dict) and step.get("uses", "").startswith("actions/checkout@"):
            return step
    raise AssertionError("job has no checkout step")


class DistinctWorkflows(unittest.TestCase):
    def setUp(self):
        self.gates_text, self.gates = _load(GATES_PATH)
        self.comment_text, self.comment = _load(COMMENT_PATH)

    def test_names_are_distinct_and_match_the_helper_constants(self):
        self.assertEqual(EXPECTED_WORKFLOW_NAME, self.gates["name"])
        self.assertEqual(PUBLISHER_WORKFLOW_NAME, self.comment["name"])
        self.assertNotEqual(self.gates["name"], self.comment["name"])
        self.assertNotEqual("Review", self.gates["name"])
        self.assertNotEqual("Review", self.comment["name"])

    def test_gate_workflow_path_is_the_expected_source(self):
        self.assertEqual(
            EXPECTED_WORKFLOW_PATH,
            str(GATES_PATH.relative_to(REPO)).replace("\\", "/"),
        )

    def test_publisher_listens_to_the_gate_workflow_not_itself(self):
        trigger = _on(self.comment)
        listened = trigger["workflow_run"]["workflows"]
        self.assertEqual([EXPECTED_WORKFLOW_NAME], listened)
        self.assertNotIn(PUBLISHER_WORKFLOW_NAME, listened)
        self.assertNotIn("Review", listened)
        self.assertEqual(["completed"], trigger["workflow_run"]["types"])

    def test_gate_workflow_does_not_listen_to_workflow_run(self):
        self.assertNotIn("workflow_run", _on(self.gates))
        self.assertNotIn("workflow_run:", self.gates_text)
        self.assertNotIn("comment", self.gates.get("jobs", {}))

    def test_gate_workflow_is_pull_request_only(self):
        self.assertIn("pull_request", _on(self.gates))
        self.assertNotIn("pull_request_target", self.gates_text)
        self.assertNotIn("pull_request_target", self.comment_text)


class PermissionAndCheckoutBoundaries(unittest.TestCase):
    def setUp(self):
        self.gates_text, self.gates = _load(GATES_PATH)
        self.comment_text, self.comment = _load(COMMENT_PATH)
        self.gate_job = _job(self.gates, "gates")
        self.comment_job = _job(self.comment, "comment")

    def test_incoming_pr_code_is_read_only_without_secrets_or_write_tokens(self):
        self.assertEqual("read", self.gates["permissions"]["contents"])
        self.assertEqual("read", self.gate_job["permissions"]["contents"])
        self.assertNotIn("pull-requests", self.gate_job.get("permissions", {}))
        self.assertNotIn("actions", self.gate_job.get("permissions", {}))
        self.assertNotIn("secrets.", self.gates_text)
        self.assertNotIn("GITHUB_TOKEN", self.gates_text)
        checkout = _checkout(self.gate_job)
        self.assertEqual("false", checkout["with"]["persist-credentials"])
        self.assertNotIn("ref", checkout.get("with", {}))

    def test_publisher_has_only_the_permissions_its_api_operations_need(self):
        permissions = self.comment_job["permissions"]
        self.assertEqual("read", permissions["contents"])
        self.assertEqual("read", permissions["actions"])
        self.assertEqual("write", permissions["pull-requests"])
        self.assertEqual({"contents", "actions", "pull-requests"}, set(permissions))
        self.assertNotIn("contents: write", self.comment_text)
        self.assertNotIn("issues: write", self.comment_text)

    def test_publisher_checkouts_trusted_github_sha_without_credentials(self):
        checkout = _checkout(self.comment_job)
        self.assertEqual("false", checkout["with"]["persist-credentials"])
        self.assertEqual("${{ github.sha }}", checkout["with"]["ref"])
        self.assertNotIn("workflow_run.head_sha", self.comment_text)
        self.assertNotIn("pull_request.head.sha", self.comment_text)

    def test_publisher_downloads_the_expected_artifact_from_the_triggering_run(self):
        download = None
        for step in _steps(self.comment_job):
            if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/download-artifact@"):
                download = step
                break
        self.assertIsNotNone(download)
        spec = download["with"]
        self.assertEqual(EXPECTED_ARTIFACT_NAME, spec["name"])
        self.assertEqual("${{ github.event.workflow_run.id }}", spec["run-id"])
        self.assertEqual("${{ github.repository }}", spec["repository"])
        self.assertEqual("${{ secrets.GITHUB_TOKEN }}", spec["github-token"])
        self.assertEqual("review-report", spec["path"])

    def test_publisher_runs_the_trusted_helper_and_not_pr_code_or_deps(self):
        self.assertIn("python3 bot/publish_comment.py --artifact-dir review-report", self.comment_text)
        self.assertNotIn("pip install", self.comment_text)
        self.assertNotIn("requirements.txt", self.comment_text)
        self.assertNotIn("unittest", self.comment_text)
        self.assertNotIn("bot/report.py corpus", self.comment_text)
        self.assertIn("name: review-report", self.gates_text)
        self.assertNotIn("pr-number.txt", self.gates_text)


if __name__ == "__main__":
    unittest.main()
