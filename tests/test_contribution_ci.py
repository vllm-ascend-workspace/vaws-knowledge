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
    git_sha,
    related_doc,
    success_class,
)
from vaws_knowledge.contribution.ci import (  # noqa: E402
    TEMPLATE_RELATIVE,
    TRUSTED_WORKFLOW_NAME,
    allowed_data_path,
    is_secret_bearing_checkout,
    maybe_merge_from_ci,
    permitted_knowledge_path,
    run_trusted_ci,
)
from vaws_knowledge.contribution.grok import ScriptedClassifier  # noqa: E402
from vaws_knowledge.contribution.recall import (  # noqa: E402
    FixtureRecall,
    OpenVikingRecall,
    UnavailableRecall,
    production_recall,
)
from vaws_knowledge.contribution.review import DECISION_CONFLICT, DECISION_NEW  # noqa: E402

TEMPLATE = REPO / TEMPLATE_RELATIVE
README = b"# Existing overview\n\nAn ordinary overview.\n"
ORDINARY = (
    "# 一次图模式启动失败的排查经验\n\n"
    "当时遇到了图模式启动失败，固定依赖后恢复。只在当时环境验证过。\n"
).encode("utf-8")
CONFLICT_NOTE = b"# Conflict candidate\n\nThis candidate conflicts with published knowledge.\n"
SECOND_NOTE = (
    "# 另一条排查经验\n\n"
    "当时换了一个编译缓存路径后恢复。只在当时环境验证过。\n"
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
        self.assertTrue(permitted_knowledge_path("corpus/note.md"))
        self.assertFalse(permitted_knowledge_path("README.md"))
        self.assertFalse(permitted_knowledge_path(".github/workflows/changed.yml"))

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
        self.github.add_markdown_tree(BASE_1, {"README.md": README})
        self.github.add_markdown_tree(HEAD_A, {"README.md": README, "corpus/note.md": ORDINARY})

    def test_unchanged_readme_is_not_selected_and_new_note_is_reviewed(self):
        classifier = ScriptedClassifier(
            by_title={"Existing overview": success_class(DECISION_CONFLICT), "一次图模式启动失败的排查经验": success_class(DECISION_NEW, reason="new")}
        )
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([related_doc("shared/other.md", "其他", "另一段不重复的正文。")]),
            classifier=classifier,
            checkout_ref="main",
            default_branch="main",
        )
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["permits_publish"])
        self.assertEqual(payload["reviewed_titles"], ["一次图模式启动失败的排查经验"])
        self.assertNotIn("Existing overview", [call[0] for call in classifier.calls])
        self.assertTrue(maybe_merge_from_ci(payload, github=self.github).merged)

    def test_untrusted_files_are_not_executed_and_block_merge(self):
        self.github.add_markdown_tree(
            HEAD_A,
            {
                "README.md": README,
                "corpus/note.md": ORDINARY,
                "steal_secrets.py": STEAL,
                ".github/workflows/exfil.yml": b"name: evil\n",
            },
        )
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
        self.assertFalse(payload["permits_publish"])
        self.assertEqual(payload["action"], "refuse_unsupported")
        self.assertFalse(payload["trusted"]["executed_pr_files"])
        omitted_paths = {item["path"] for item in payload["fetch"]["omitted"]}
        self.assertIn("steal_secrets.py", omitted_paths)
        self.assertIn(".github/workflows/exfil.yml", omitted_paths)
        self.assertFalse((self.stash / "steal_secrets.py").exists())
        self.assertTrue((self.stash / "corpus" / "note.md").is_file())
        self.assertFalse(maybe_merge_from_ci(payload, github=self.github).merged)

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

    def test_two_changed_notes_classify_the_conflict_and_do_not_merge(self):
        old_keep = "# 另一条排查经验\n\n旧的缓存路径说明。\n".encode("utf-8")
        self.github.add_markdown_tree(BASE_1, {"README.md": README, "corpus/keep.md": old_keep})
        self.github.add_markdown_tree(
            HEAD_A,
            {"README.md": README, "corpus/keep.md": SECOND_NOTE, "corpus/candidate.md": CONFLICT_NOTE},
        )
        classifier = ScriptedClassifier(
            by_title={
                "Conflict candidate": success_class(DECISION_CONFLICT, reason="opposite claim"),
                "另一条排查经验": success_class(DECISION_NEW, reason="new"),
            }
        )
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([related_doc("shared/graph.md", "图模式启动", "条件：同一构建。图模式可以稳定启动。")]),
            classifier=classifier,
            checkout_ref="main",
            default_branch="main",
        )
        self.assertEqual(
            set(payload.get("reviewed_titles") or []),
            {"Conflict candidate", "另一条排查经验"},
        )
        self.assertEqual(payload["decision"], DECISION_CONFLICT)
        self.assertFalse(payload["permits_publish"])
        self.assertFalse(maybe_merge_from_ci(payload, github=self.github).merged)

    def test_unsupported_changed_workflow_refuses_the_whole_pr(self):
        self.github.add_markdown_tree(
            HEAD_A,
            {
                "README.md": README,
                "corpus/candidate.md": CONFLICT_NOTE,
                ".github/workflows/changed.yml": b"name: changed\n",
            },
        )
        classifier = ScriptedClassifier(
            by_title={
                "Existing overview": success_class(DECISION_NEW),
                "Conflict candidate": success_class(DECISION_CONFLICT, reason="opposite claim"),
            }
        )
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=FixtureRecall([]),
            classifier=classifier,
            checkout_ref="main",
            default_branch="main",
        )
        self.assertEqual([call[0] for call in classifier.calls], ["Conflict candidate"])
        self.assertIn("Conflict candidate", payload.get("reviewed_titles") or [])
        self.assertNotEqual(payload.get("title"), "Existing overview")
        self.assertFalse(payload["permits_publish"])
        self.assertIn(".github/workflows/changed.yml", payload.get("unsupported_changes") or [])
        merged = maybe_merge_from_ci(payload, github=self.github)
        self.assertFalse(merged.merged)

    def test_truncated_change_list_does_not_permit_publish(self):
        self.github.add_markdown_tree(HEAD_A, {"README.md": README, "corpus/note.md": ORDINARY}, truncated=True)
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
        self.assertFalse(payload["permits_publish"])
        self.assertIn("truncated", payload["reason"])
        self.assertFalse(maybe_merge_from_ci(payload, github=self.github).merged)

    def test_unavailable_recall_does_not_permit_publish(self):
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            recall=UnavailableRecall("OpenViking URL is not configured"),
            classifier=ScriptedClassifier(success_class(DECISION_NEW)),
            checkout_ref="main",
            default_branch="main",
        )
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["permits_publish"])
        self.assertFalse(maybe_merge_from_ci(payload, github=self.github).merged)

    def test_default_ci_path_does_not_substitute_empty_fixture_recall(self):
        payload = run_trusted_ci(
            _event(),
            github=self.github,
            repository=OWNER_REPO,
            stash=self.stash,
            classifier=ScriptedClassifier(success_class(DECISION_NEW)),
            checkout_ref="main",
            default_branch="main",
            environ={},
        )
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["permits_publish"])
        self.assertIn("OpenViking", payload["reason"])


class RecallConfig(unittest.TestCase):
    def test_missing_url_is_unavailable_not_empty_success(self):
        recall = production_recall(base_sha=BASE_1, environ={})
        self.assertIsInstance(recall, UnavailableRecall)
        result = recall.related("图模式")
        self.assertFalse(result.ok)
        self.assertEqual(result.documents, [])

    def test_injected_client_empty_find_is_verified_empty(self):
        class Client:
            def find(self, query, target_uri, limit, options):
                del query, target_uri, limit, options
                return {"resources": []}

        recall = production_recall(base_sha=BASE_1, client=Client())
        self.assertIsInstance(recall, OpenVikingRecall)
        result = recall.related("图模式")
        self.assertTrue(result.ok)
        self.assertEqual(result.documents, [])

    def test_mismatched_corpus_sha_is_unavailable(self):
        class Client:
            def find(self, *args, **kwargs):
                return {"resources": []}

        recall = production_recall(base_sha=BASE_1, corpus_sha=git_sha("other-base"), client=Client())
        self.assertIsInstance(recall, UnavailableRecall)
        self.assertIn("does not match", recall.reason)


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
        self.assertIn("@<pin>", self.text)
        self.assertTrue(TEMPLATE.name.endswith(".tmpl"))
