"""What the deterministic gates are allowed to conclude.

The central rule of this repository is that automation can gate but cannot
establish truth. Passing every check places an entry in `corpus/unverified/`
and nothing more; reaching `corpus/verified/` needs a followable evidence
reference and a confirmation from somebody other than the submitter.

That rule is easy to state and easy to erode — one convenience commit that lets
a fully green run write into the verified corpus and the repository becomes a
machine for publishing unreviewed claims with a bot's endorsement attached. So
the rule is asserted here directly, against a run where every gate passes.

The other half is fail-closed behaviour. A gate whose tool is missing must be a
failure, never a pass, because "we could not check" and "we checked and it was
fine" are the two states a review pipeline must never confuse.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bot import gates  # noqa: E402


def _interpreter_with_dependencies() -> str | None:
    """An interpreter that can actually run the external gates.

    The gates shell out to tools/ which needs jsonschema. A bare `python3`
    usually does not have it, and that is not a bug — the gate correctly fails
    closed when its tool cannot run. But asserting what a *green* run permits
    needs a green run, so the tests that do that look for a usable interpreter
    and skip with a stated reason when there is none, rather than asserting
    against a red report and quietly proving nothing.
    """
    import subprocess

    for candidate in (sys.executable, str(REPO / ".venv" / "bin" / "python3")):
        if not candidate:
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import jsonschema, yaml"], capture_output=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return None


class GreenRunStillCannotPublish(unittest.TestCase):
    def setUp(self):
        python = _interpreter_with_dependencies()
        if python is None:
            self.skipTest(
                "no interpreter with jsonschema available; create .venv and "
                "install requirements.txt to exercise the external gates"
            )
        self.report = gates.run_gates(["examples"], mode="pr", root=REPO, python=python)

    def test_the_reference_corpus_passes_every_gate(self):
        # If this fails the repository's own example does not satisfy its own
        # checks, and every assertion below is meaningless.
        failing = [r for r in self.report["gates"] if r["blocking"] and r["status"] != "pass"]
        self.assertEqual([], failing, self.report)
        self.assertEqual("pass", self.report["overall"])

    def test_a_fully_green_run_permits_only_the_unverified_zone(self):
        permits = self.report["permits"]
        self.assertIn("unverified", permits)
        self.assertNotIn("corpus/verified", permits)

    def test_the_report_says_out_loud_that_it_did_not_establish_truth(self):
        # A reader skimming a green check should not have to know the promotion
        # rules to avoid over-reading the result.
        self.assertIn("never establishes truth", self.report["permits"])


class FailClosed(unittest.TestCase):
    def test_a_missing_gate_tool_fails_rather_than_skips(self):
        # `python` is the interpreter used to invoke the external gate scripts.
        # Pointing it at something that cannot run stands in for the tools being
        # absent, which is the realistic case: sync/, tools/ and conformance/
        # are separate packages and a checkout can be missing any of them.
        report = gates.run_gates(
            ["examples"], mode="pr", root=REPO, python="/nonexistent/python-interpreter"
        )
        external = [g for g in report["gates"] if g["id"] in {"schema", "redaction"}]
        self.assertTrue(external, "the external gates should still be reported, not omitted")
        for gate in external:
            with self.subTest(gate=gate["id"]):
                self.assertNotEqual("pass", gate["status"], gate)
                self.assertTrue(gate["blocking"], gate)
        self.assertEqual("fail", report["overall"])

    def test_an_invalid_policy_file_does_not_silently_pass_the_gates_it_configures(self):
        report = gates.run_gates(
            ["examples"], mode="pr", root=REPO, policy_path="tests/fixtures/bot/does-not-exist.yaml"
        )
        configured = [g for g in report["gates"] if g["id"] in {"integrity", "duplicates", "conflicts", "staleness"}]
        self.assertTrue(configured)
        for gate in configured:
            with self.subTest(gate=gate["id"]):
                self.assertNotEqual("pass", gate["status"], gate)


class ReportIsDeterministic(unittest.TestCase):
    def test_two_runs_render_identically(self):
        # The workflow updates a single review comment instead of posting a new
        # one. That only works if the rendering is stable, otherwise every run
        # produces a spurious edit and reviewers learn to ignore the comment.
        from bot import report as report_mod

        first = gates.run_gates(["examples"], mode="pr", root=REPO, as_of="2026-09-07")
        second = gates.run_gates(["examples"], mode="pr", root=REPO, as_of="2026-09-07")
        self.assertEqual(
            report_mod.render_markdown(first),
            report_mod.render_markdown(second),
        )


if __name__ == "__main__":
    unittest.main()
