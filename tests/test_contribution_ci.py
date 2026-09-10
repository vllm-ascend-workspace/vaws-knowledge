"""Trusted CI: PR files as data, template boundaries, untrusted content."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required: {exc}") from exc

from contribution.support import (  # noqa: E402
    BASE_1,
    HEAD_A,
    OWNER_REPO,
    FakeContributionGitHub,
    related_doc,
    success_class,
)
from vaws_knowledge.contribution.ci import (  # noqa: E402
    TEMPLATE_RELATIVE,
    TRUSTED_WORKFLOW_NAME,
    allowed_data_path,
    is_secret_bearing_checkout,
    run_trusted_ci,
)
from vaws_knowledge.contribution.grok import ScriptedClassifier  # noqa: E402
from vaws_knowledge.contribution.recall import FixtureRecall  # noqa: E402
from vaws_knowledge.contribution.review import DECISION_NEW  # noqa: E402

TEMPLATE = REPO / TEMPLATE_RELATIVE
ORDINARY = (
    "# 一次图模式启动失败的排查经验\n\n"
    "当时遇到了图模式启动失败，固定依赖后恢复。只在当时环境验证过。\n"
).encode("utf-8")
STEAL = b'raise SystemExit("untrusted pull-request code executed")\n'


def _event(head: str = HEAD_A, base: str = BASE_1, number: int = 9) -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": number,
            "state": "open",
            "head": {"sha": head, "repo": {"full_name": OWNER_REPO}},
            "base": {"sha": base, "ref": "main", "repo": {"full_name": OWNER_REPO}},
        },
        "repository": {"full_name": OWNER_REPO, "default_branch": "main"},
    }


class PathPolicy(unittest.TestCase):
    def test_markdown_is_data_and_workflows_are_not(self):
        self.assertIsNone(allowed_data_path("corpus/note.md"))
        self.assertEqual(allowed_data_path(".github/workflows/evil.yml"), "secret_bearing_or_installable")
        self.assertEqual(allowed_data_path("steal_secrets.py"), "not_markdown_data")
        self.assertEqual(allowed_data_path("../etc/passwd.md"), "path_traversal")

    def test_secret_job_must_not_checkout_pr_head(self):
        self.assertTrue(is_secret_bearing_checkout(HEAD_A, default_branch="main", event_head=HEAD_A))
        self.assertFalse(
            is_secret_bearing_checkout("main", default_branch="main", event_head=HEAD_A)
        )


class TrustedRunner(unittest.TestCase):
    def setUp(self):
        self.stash = pathlib.Path(tempfile.mkdtemp())
        self.github = FakeContributionGitHub(base_sha=BASE_1)
        self.github.add_pull(number=9, head=HEAD_A, base=BASE_1)
        self.github.add_markdown_tree(
            HEAD_A,
            {
                "corpus/note.md": ORDINARY,
                "steal_secrets.py": STEAL,
                ".github/workflows/exfil.yml": b"name: evil\n",
            },
        )

    def test_untrusted_files_are_not_executed_and_markdown_is_reviewed(self):
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([related_doc("shared/other.md", "其他", "另一段不重复的正文。")]),
            classifier=ScriptedClassifier(success_class(DECISION_NEW, reason="new")),
            checkout_ref="main",
            default_branch="main",
        )
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["permits_publish"])
        self.assertFalse(payload["trusted"]["executed_pr_files"])
        self.assertFalse(payload["trusted"]["installed_fork"])
        omitted_paths = {item["path"] for item in payload["fetch"]["omitted"]}
        self.assertIn("steal_secrets.py", omitted_paths)
        self.assertIn(".github/workflows/exfil.yml", omitted_paths)
        steal = self.stash / "steal_secrets.py"
        self.assertFalse(steal.exists())
        self.assertTrue((self.stash / "corpus" / "note.md").is_file())

    def test_refusing_to_run_fork_code_is_explicit(self):
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([]),
            classifier=ScriptedClassifier(success_class(DECISION_NEW)),
            checkout_ref="main",
            default_branch="main",
            execute_paths=["steal_secrets.py"],
        )
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["permits_publish"])
        self.assertIn("must not execute", payload["reason"])

    def test_checkout_of_pr_head_is_refused(self):
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([]),
            classifier=ScriptedClassifier(success_class(DECISION_NEW)),
            checkout_ref=HEAD_A,
            default_branch="main",
        )
        self.assertEqual(payload["status"], "error")
        self.assertIn("must not checkout pull-request head", payload["reason"])

    def test_permission_error_is_not_a_pass(self):
        self.github.auth_fail = True
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([]),
            classifier=ScriptedClassifier(success_class(DECISION_NEW)),
            checkout_ref="main",
            default_branch="main",
        )
        self.assertNotEqual(payload.get("status"), "success")
        self.assertFalse(payload.get("permits_publish"))
        self.assertIn("permission", payload["reason"].lower())


class Template(unittest.TestCase):
    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")
        self.data = yaml.load(self.text, Loader=yaml.BaseLoader)
        if not isinstance(self.data, dict):
            raise AssertionError("template did not load as a mapping")

    def test_template_is_not_an_enabled_workflow(self):
        self.assertTrue(TEMPLATE.is_file())
        self.assertNotEqual(TEMPLATE.parent, REPO / ".github" / "workflows")
        self.assertIn("TEMPLATE ONLY", self.text)
        self.assertEqual(self.data.get("name"), TRUSTED_WORKFLOW_NAME)

    def test_secret_job_checks_out_default_branch_not_pr_head(self):
        jobs = self.data.get("jobs")
        self.assertIsInstance(jobs, dict)
        review = jobs.get("review")
        self.assertIsInstance(review, dict)
        steps = review.get("steps")
        self.assertIsInstance(steps, list)
        checkout = next(
            step
            for step in steps
            if isinstance(step, dict) and str(step.get("uses") or "").startswith("actions/checkout@")
        )
        with_ = checkout.get("with") or {}
        self.assertEqual(with_.get("persist-credentials"), "false")
        self.assertIn("default_branch", str(with_.get("ref")))
        self.assertNotIn("pull_request.head", str(with_.get("ref")))
        joined = "\n".join(str(step) for step in steps)
        self.assertNotIn("pip install -e", joined)
        self.assertNotIn("pip install ./", joined)
        self.assertIn("vaws-knowledge==", joined)
        self.assertIn("python -m vaws_knowledge.contribution ci", joined)
