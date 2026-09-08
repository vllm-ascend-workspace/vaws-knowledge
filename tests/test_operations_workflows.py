"""Workflow names, triggers, permissions and checkout boundaries for Stage 2."""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
from vaws_knowledge.bot.advisory_review import (  # noqa: E402
    ADVISORY_ARTIFACT_NAME,
    ADVISORY_WORKFLOW_NAME,
    ADVISORY_WORKFLOW_PATH,
)
from vaws_knowledge.bot.publish_comment import EXPECTED_WORKFLOW_NAME, PUBLISHER_WORKFLOW_NAME  # noqa: E402

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required to read workflow fixtures: {exc}") from exc

COLLECT_PATH = REPO / ".github" / "workflows" / "collect.yml"
ADVISORY_PATH = REPO / ".github" / "workflows" / "advisory-review.yml"
SNAPSHOT_PATH = REPO / ".github" / "workflows" / "publish-snapshot.yml"
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


def _checkouts(job: dict) -> list[dict]:
    found = []
    for step in _steps(job):
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout@"):
            found.append(step)
    return found


class NamesAndTriggers(unittest.TestCase):
    def setUp(self):
        self.collect_text, self.collect = _load(COLLECT_PATH)
        self.advisory_text, self.advisory = _load(ADVISORY_PATH)
        self.snapshot_text, self.snapshot = _load(SNAPSHOT_PATH)
        self.gates_text, self.gates = _load(GATES_PATH)
        self.comment_text, self.comment = _load(COMMENT_PATH)

    def test_workflow_names_are_distinct(self):
        names = [
            self.collect["name"],
            self.advisory["name"],
            self.snapshot["name"],
            self.gates["name"],
            self.comment["name"],
        ]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual("Central collection", self.collect["name"])
        self.assertEqual(ADVISORY_WORKFLOW_NAME, self.advisory["name"])
        self.assertEqual("Verified snapshot", self.snapshot["name"])
        self.assertEqual(EXPECTED_WORKFLOW_NAME, self.gates["name"])
        self.assertEqual(PUBLISHER_WORKFLOW_NAME, self.comment["name"])
        self.assertEqual(
            ADVISORY_WORKFLOW_PATH,
            str(ADVISORY_PATH.relative_to(REPO)).replace("\\", "/"),
        )

    def test_collect_has_daily_schedule_and_dispatch_modes(self):
        trigger = _on(self.collect)
        self.assertIn("schedule", trigger)
        cron = trigger["schedule"]
        self.assertEqual("27 4 * * *", cron[0]["cron"])
        self.assertIn("workflow_dispatch", trigger)
        mode = trigger["workflow_dispatch"]["inputs"]["mode"]
        self.assertEqual(["preview", "propose"], mode["options"])
        self.assertEqual("preview", mode["default"])
        self.assertIn("04:27", self.collect_text)
        self.assertIn("off-hour", self.collect_text)
        self.assertIn("1196723340", self.collect_text)

    def test_collect_has_concurrency_timeout_and_bounds(self):
        self.assertIn("knowledge-central-collection-", self.collect["concurrency"]["group"])
        self.assertEqual("false", self.collect["concurrency"]["cancel-in-progress"])
        self.assertEqual("30", _job(self.collect, "collect")["timeout-minutes"])
        self.assertEqual("30", _job(self.collect, "propose")["timeout-minutes"])
        self.assertIn("VAWS_COLLECT_MAX_PAGES", self.collect_text)
        self.assertIn("VAWS_COLLECT_MAX_FORKS", self.collect_text)
        self.assertIn("VAWS_COLLECT_MAX_FILES_PER_REPO", self.collect_text)
        self.assertIn("VAWS_COLLECT_MAX_FILE_BYTES", self.collect_text)
        self.assertIn("VAWS_COLLECT_MAX_TOTAL_BYTES", self.collect_text)

    def test_advisory_listens_to_review_gates_not_itself(self):
        trigger = _on(self.advisory)
        self.assertEqual([EXPECTED_WORKFLOW_NAME], trigger["workflow_run"]["workflows"])
        self.assertNotIn(ADVISORY_WORKFLOW_NAME, trigger["workflow_run"]["workflows"])
        self.assertNotIn("Review", trigger["workflow_run"]["workflows"])
        self.assertEqual(["completed"], trigger["workflow_run"]["types"])
        self.assertNotIn("workflow_run", _on(self.collect))
        self.assertNotIn("pull_request_target", self.collect_text)
        self.assertNotIn("pull_request_target", self.advisory_text)
        self.assertNotIn("pull_request_target", self.snapshot_text)

    def test_snapshot_is_schedule_plus_dispatch_and_verified_only(self):
        trigger = _on(self.snapshot)
        self.assertIn("schedule", trigger)
        self.assertEqual("33 5 * * *", trigger["schedule"][0]["cron"])
        self.assertIn("workflow_dispatch", trigger)
        self.assertIn("vaws_knowledge.sync.snapshot", self.snapshot_text)
        self.assertIn("corpus/unverified", self.snapshot_text)
        self.assertIn("verified-snapshot", self.snapshot_text)
        self.assertNotIn("--skip-gates", self.snapshot_text)

    def test_opt_in_pull_template_lives_under_docs(self):
        path = REPO / "docs" / "periodic-pull-template.yml"
        text, data = _load(path)
        self.assertEqual("Pull verified knowledge snapshot", data["name"])
        self.assertIn("workflow_dispatch", _on(data))
        self.assertEqual("read", data["permissions"]["contents"])
        self.assertNotIn("contents: write", text)
        self.assertIn(".vaws-local/knowledge/shared/", text)
        federation = (REPO / "docs" / "federation.md").read_text(encoding="utf-8")
        self.assertIn("docs/periodic-pull-template.yml", federation)
        self.assertIn("docs/periodic-pull-template.yml", (REPO / "docs" / "operations.md").read_text(encoding="utf-8"))
        self.assertIn("knowledge_shared_cache.py", federation)
        self.assertIn("knowledge_shared_cache.py", text)
        self.assertNotIn("VAWS_KNOWLEDGE_SHARED_ROOTS", federation)
        self.assertIn("does not refresh", text.lower() + " " + "does not refresh a cache")


class PermissionBoundaries(unittest.TestCase):
    def setUp(self):
        self.collect_text, self.collect = _load(COLLECT_PATH)
        self.advisory_text, self.advisory = _load(ADVISORY_PATH)
        self.snapshot_text, self.snapshot = _load(SNAPSHOT_PATH)

    def test_collect_job_is_read_only_and_preview_never_writes(self):
        collect_job = _job(self.collect, "collect")
        self.assertEqual("read", self.collect["permissions"]["contents"])
        self.assertEqual("read", collect_job["permissions"]["contents"])
        self.assertNotIn("pull-requests", collect_job.get("permissions", {}))
        self.assertIn("--mode preview", self.collect_text)
        checkout = _checkouts(collect_job)[0]
        self.assertEqual("false", checkout["with"]["persist-credentials"])

    def test_propose_job_has_write_only_where_needed_and_skips_preview(self):
        propose = _job(self.collect, "propose")
        self.assertEqual("write", propose["permissions"]["contents"])
        self.assertEqual("write", propose["permissions"]["pull-requests"])
        self.assertIn("github.event.inputs.mode == 'propose'", propose["if"])
        self.assertIn("github.event_name == 'schedule'", propose["if"])
        self.assertIn("--mode propose", self.collect_text)
        self.assertNotIn("--skip-gates", self.collect_text)
        self.assertNotIn("--drop-undeclared", self.collect_text)
        self.assertNotIn("--allow-duplicate-candidates", self.collect_text)
        self.assertIn("GitHub-required approval", self.collect_text)
        self.assertIn("collect-handoff", self.collect_text)
        self.assertIn("--handoff-dir", self.collect_text)
        self.assertIn("CURRENT_SUCCESS_DIR", self.collect_text)
        self.assertNotIn("name: collect-exports", self.collect_text)

    def test_secret_bearing_advisory_job_does_not_checkout_pr_code(self):
        advisory = _job(self.advisory, "advisory")
        self.assertEqual("read", advisory["permissions"]["contents"])
        self.assertNotIn("pull-requests", advisory.get("permissions", {}))
        self.assertIn("secrets.XAI_API_KEY", self.advisory_text)
        self.assertIn("vars.XAI_MODEL", self.advisory_text)
        checkout = _checkouts(advisory)[0]
        self.assertEqual("false", checkout["with"]["persist-credentials"])
        self.assertEqual("${{ github.sha }}", checkout["with"]["ref"])
        self.assertNotIn("workflow_run.head_sha", checkout.get("with", {}))
        self.assertNotIn("pull_request.head.sha", self.advisory_text)
        comment = _job(self.advisory, "comment")
        self.assertEqual("write", comment["permissions"]["pull-requests"])
        self.assertNotIn("XAI_API_KEY", "".join(
            str(step.get("env", "")) for step in _steps(comment) if isinstance(step, dict)
        ))
        self.assertIn(ADVISORY_ARTIFACT_NAME, self.advisory_text)
        self.assertIn("vaws_knowledge.bot.advisory_review", self.advisory_text)

    def test_snapshot_job_is_read_only(self):
        job = _job(self.snapshot, "snapshot")
        self.assertEqual("read", job["permissions"]["contents"])
        checkout = _checkouts(job)[0]
        self.assertEqual("false", checkout["with"]["persist-credentials"])


if __name__ == "__main__":
    unittest.main()
