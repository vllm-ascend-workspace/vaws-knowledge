"""sync/rescan.py: re-scan below the new profile, quarantine by proposal, edit nothing."""

import json
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common, rescan_mod  # noqa: E402


class Rescan(synctest.SyncTestCase):
    def test_quarantines_by_proposal_and_edits_nothing(self):
        before = synctest.dir_digest(self.corpus_dir)
        report = rescan_mod.rescan(self.corpus(), "r2", tools_dir=synctest.TOOLS_MARKER, day="2026-09-10")
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir), "rescan never rewrites the corpus")
        self.assertEqual([synctest.UUID_MARKER], [i.uuid for i in report.quarantine()])
        item = report.quarantine()[0]
        self.assertEqual("verified", item.layer)
        self.assertEqual("corpus/verified/known-failure-signatures.yaml", item.path)
        self.assertTrue(any("history" in step for step in item.remediation))
        self.assertTrue(any("re-publish" in step for step in item.remediation))
        self.assertIn(synctest.UUID_MARKER, self.corpus().index, "still present until a human removes it")

    def test_entries_at_or_above_the_target_profile_are_not_rescanned(self):
        report = rescan_mod.rescan(self.corpus(), "r2", tools_dir=synctest.TOOLS_MARKER)
        scanned = {i.uuid for i in report.items}
        self.assertNotIn(synctest.UUID_CURRENT, scanned)
        self.assertEqual(1, report.skipped_current)
        self.assertEqual({synctest.UUID_VERIFIED, synctest.UUID_MARKER, synctest.UUID_UNVERIFIED}, scanned)

    def test_profile_comparison_is_numeric(self):
        report = rescan_mod.rescan(self.corpus(), "r10", tools_dir=synctest.TOOLS_MARKER)
        self.assertIn(synctest.UUID_CURRENT, {i.uuid for i in report.items}, "r2 < r10")

    def test_findings_are_withheld_unless_requested(self):
        report = rescan_mod.rescan(self.corpus(), "r2", tools_dir=synctest.TOOLS_MARKER)
        self.assertIsNone(report.quarantine()[0].findings)
        self.assertNotIn("QUARANTINE-ME", json.dumps(report.to_public()))
        report = rescan_mod.rescan(self.corpus(), "r2", tools_dir=synctest.TOOLS_MARKER, include_findings=True)
        self.assertIn("QUARANTINE-ME", report.quarantine()[0].findings)

    def test_missing_redaction_tool_fails_closed(self):
        with self.assertRaises(_common.SyncError) as ctx:
            rescan_mod.rescan(self.corpus(), "r2", tools_dir=synctest.TOOLS_MISSING)
        self.assertIn("gate unavailable", str(ctx.exception))

    def test_malformed_profile_is_rejected(self):
        with self.assertRaises(_common.SyncError):
            rescan_mod.rescan(self.corpus(), "v2", tools_dir=synctest.TOOLS_MARKER)

    def test_nothing_to_quarantine_under_a_lenient_ruleset(self):
        report = rescan_mod.rescan(self.corpus(), "r2", tools_dir=synctest.TOOLS_PASS)
        self.assertEqual([], report.quarantine())
        self.assertEqual(3, len(report.passed()))


class RescanCli(synctest.SyncTestCase):
    def test_writes_proposal_json_and_exit_zero(self):
        out = self.tmp / "rescan-r2.json"
        before = synctest.dir_digest(self.corpus_dir)
        proc = subprocess.run(
            [sys.executable, str(synctest.SYNC_DIR / "rescan.py"), "--corpus", str(self.corpus_dir),
             "--tools-dir", str(synctest.TOOLS_MARKER), "--profile", "r2", "--out", str(out), "--today", "2026-09-10"],
            capture_output=True, text=True,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))
        data = json.loads(out.read_text())
        self.assertEqual("redaction-rescan", data["proposal"])
        self.assertEqual({"scanned": 3, "passed": 2, "quarantine": 1, "skipped_current": 1}, data["counts"])
        self.assertEqual([synctest.UUID_MARKER], [q["uuid"] for q in data["quarantine"]])
        self.assertNotIn(str(self.tmp), out.read_text(), "no machine-specific paths in the proposal")
        self.assertIn("nothing was changed", proc.stdout)

    def test_gate_unavailable_exit_code(self):
        proc = subprocess.run(
            [sys.executable, str(synctest.SYNC_DIR / "rescan.py"), "--corpus", str(self.corpus_dir),
             "--tools-dir", str(synctest.TOOLS_MISSING), "--profile", "r2"],
            capture_output=True, text=True,
        )
        self.assertEqual(_common.EXIT_GATE, proc.returncode)
        self.assertIn("gate unavailable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
