"""sync/plan.py decides every idempotency rule in docs/federation.md and
changes nothing while doing so."""

import copy
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common, plan_mod  # noqa: E402


class PlanDecisions(synctest.SyncTestCase):
    def _single(self, plan):
        self.assertEqual(1, len(plan.items), plan.items)
        return plan.items[0]

    def test_unchanged_reexport_is_a_noop(self):
        export = self.make_export([self.entry(synctest.UUID_UNVERIFIED)])
        item = self._single(self.plan([export]))
        self.assertEqual("no-op", item.action)

    def test_reexport_with_different_processing_metadata_is_still_a_noop(self):
        # Re-verifying, re-reviewing or renaming must not look like a revision.
        e = self.entry(synctest.UUID_UNVERIFIED)
        e["slug"] = "renamed-by-fork"
        e["verification"]["last_verified_at"] = "2030-01-01"
        e["lifecycle"]["updated_at"] = "2030-01-01"
        item = self._single(self.plan([self.make_export([e])]))
        self.assertEqual("no-op", item.action)

    def test_changed_claim_is_a_revision_of_the_same_uuid(self):
        e = self.entry(synctest.UUID_UNVERIFIED)
        before = self.entry(synctest.UUID_UNVERIFIED)
        e["rule"]["resolution"] += " Confirmed on a second host class."
        item = self._single(self.plan([self.make_export([e])], day="2026-09-09"))
        self.assertEqual("revision", item.action)
        self.assertEqual(synctest.UUID_UNVERIFIED, item.entry_after["uuid"])
        self.assertEqual(_common.content_hash(e), item.entry_after["content_hash"])
        self.assertEqual("2026-09-09", item.entry_after["lifecycle"]["updated_at"])
        self.assertEqual(before["lifecycle"]["first_seen"], item.entry_after["lifecycle"]["first_seen"])
        self.assertEqual(
            before["verification"]["last_verified_at"],
            item.entry_after["verification"]["last_verified_at"],
            "rewording is not re-verifying",
        )

    def test_revision_keeps_main_repo_owned_fields(self):
        e = self.entry(synctest.UUID_UNVERIFIED)
        e["rule"]["summary"] = "Reworded summary for the graph capture warmup failure"
        e["status"] = "verified"  # the fork may claim this; the corpus decides
        e["lifecycle"]["resolved_by"] = {"type": "commit", "ref": "abc"}
        e["verification"]["verified_by"] = ["someone-in-the-fork"]
        item = self._single(self.plan([self.make_export([e])]))
        self.assertEqual("revision", item.action)
        after = item.entry_after
        self.assertEqual("unverified", after["status"])
        self.assertIsNone(after["lifecycle"]["resolved_by"])
        self.assertEqual(["example-reviewer"], after["verification"]["verified_by"])
        self.assertEqual("fork-a/vllm-ascend-workspace", after["provenance"]["origin_repo"],
                         "provenance records which fork proposed this revision")

    def test_unknown_uuid_is_new_and_targets_unverified(self):
        item = self._single(self.plan([self.make_export([self.new_entry("alpha")])]))
        self.assertEqual("new", item.action)
        self.assertEqual("corpus/unverified/known-failure-signatures.yaml", item.target_path)

    def test_new_entry_status_is_normalized_to_unverified(self):
        e = self.new_entry("beta")
        e["status"], e["confidence"] = "verified", "high"
        item = self._single(self.plan([self.make_export([e])]))
        self.assertEqual("new", item.action)
        self.assertEqual("unverified", item.entry_after["status"])
        self.assertEqual("medium", item.entry_after["confidence"])
        self.assertIn("verification", item.entry_after, "the evidence is kept for the reviewer")
        self.assertEqual(_common.content_hash(e), item.entry_after["content_hash"],
                         "status is not part of the revision")

    def test_new_entry_in_unknown_kind_targets_a_new_document(self):
        e = self.new_entry("gamma")
        item = self._single(self.plan([self.make_export([e], kind="version-compatibility")]))
        self.assertEqual("new", item.action)
        self.assertEqual("corpus/unverified/version-compatibility.yaml", item.target_path)

    def test_near_duplicate_under_a_different_uuid_is_reported_not_merged(self):
        dup = self.entry(synctest.UUID_VERIFIED)
        dup["uuid"] = synctest.fresh_uuid("dup")
        dup["slug"] = "same-claim-different-identity"
        dup["rule"]["summary"] = dup["rule"]["summary"] + " (seen again)"
        item = self._single(self.plan([self.make_export([dup])]))
        self.assertEqual("duplicate-candidate", item.action)
        self.assertEqual([synctest.UUID_VERIFIED], item.related, "both sides are reported")
        self.assertIn("humans decide", item.reason)

    def test_exact_rule_copy_is_a_duplicate_candidate(self):
        dup = self.entry(synctest.UUID_UNVERIFIED)
        dup["uuid"] = synctest.fresh_uuid("dup2")
        item = self._single(self.plan([self.make_export([dup])]))
        self.assertEqual("duplicate-candidate", item.action)

    def test_two_near_identical_new_entries_in_one_export_flag_the_second(self):
        a = self.new_entry("delta")
        b = self.new_entry("delta")
        b["uuid"] = synctest.fresh_uuid("delta-second")
        plan = self.plan([self.make_export([a, b])])
        self.assertEqual(["new", "duplicate-candidate"], [i.action for i in plan.items])
        self.assertEqual([a["uuid"]], plan.items[1].related)

    def test_genuinely_different_entries_are_not_duplicates(self):
        other = self.new_entry(
            "zeta",
            summary="Weight loading stalls when the safetensors index lists a missing shard",
            symptom="Loading reaches the last shard and waits forever with no error message",
            root_cause="The loader retries a missing file indefinitely instead of failing",
            resolution="Regenerate the index so every listed shard exists before serving",
            fingerprints=["weight loading stall last shard", "safetensors index missing shard"],
        )
        plan = self.plan([self.make_export([self.new_entry("eps"), other])])
        self.assertEqual(["new", "new"], [i.action for i in plan.items])

    def test_hash_mismatch_is_a_conflict(self):
        e = self.entry(synctest.UUID_UNVERIFIED)
        e["rule"]["resolution"] += " edited by hand without rehashing"
        item = self._single(self.plan([self.make_export([e], rehash=False)]))
        self.assertEqual("conflict", item.action)
        self.assertIn("content_hash mismatch", item.reason)

    def test_revision_targeting_verified_is_a_conflict(self):
        e = self.entry(synctest.UUID_VERIFIED)
        e["rule"]["resolution"] += " Narrowed after a conflict report."
        item = self._single(self.plan([self.make_export([e])]))
        self.assertEqual("conflict", item.action)
        self.assertEqual("verified", item.current_layer)
        self.assertIn("corpus/verified/", item.reason)

    def test_kind_mismatch_is_a_conflict(self):
        e = self.entry(synctest.UUID_UNVERIFIED)
        item = self._single(self.plan([self.make_export([e], kind="version-compatibility")]))
        self.assertEqual("conflict", item.action)

    def test_repeated_uuid_inside_the_export_is_a_conflict(self):
        e = self.new_entry("eta")
        plan = self.plan([self.make_export([e, e])])
        self.assertEqual(["conflict", "conflict"], [i.action for i in plan.items])

    def test_malformed_uuid_is_a_conflict(self):
        e = self.new_entry("theta")
        e["uuid"] = "not-a-uuid"
        item = self._single(self.plan([self.make_export([e])]))
        self.assertEqual("conflict", item.action)

    def test_mixed_rule_measurement_reference_roundtrip_is_not_a_false_duplicate(self):
        packaged_ref = synctest.yaml.safe_load(
            (synctest.REPO / "corpus" / "unverified" / "sourced-references.yaml").read_text(
                encoding="utf-8"
            )
        )["entries"][0]
        (self.corpus_dir / "unverified" / "sourced-references.yaml").write_text(
            _common.dump_yaml(
                {
                    "schema_version": 2,
                    "kind": "sourced-references",
                    "layer": "unverified",
                    "updated_at": "2026-09-10",
                    "entries": [copy.deepcopy(packaged_ref)],
                }
            ),
            encoding="utf-8",
        )
        rule = self.new_entry("mixed-rule")
        measurement = copy.deepcopy(
            synctest.yaml.safe_load(
                (synctest.REPO / "corpus" / "unverified" / "hardware-measurements-sustained.yaml").read_text(
                    encoding="utf-8"
                )
            )["entries"][0]
        )
        measurement["uuid"] = synctest.fresh_uuid("mixed-meas")
        measurement["slug"] = "mixed-measurement"
        measurement["content_hash"] = _common.content_hash(measurement)
        other_ref = copy.deepcopy(packaged_ref)
        other_ref["uuid"] = synctest.fresh_uuid("mixed-ref")
        other_ref["slug"] = "mixed-other-reference"
        other_ref["reference"] = copy.deepcopy(packaged_ref["reference"])
        other_ref["reference"]["summary"] = "JSON-RPC 2.0 specifies batched arrays of requests"
        other_ref["reference"]["text"] = (
            "The JSON-RPC 2.0 specification describes batch requests as arrays. "
            "That is a different citation from MCP stdio newline framing."
        )
        other_ref["reference"]["source"] = {
            "title": "JSON-RPC 2.0 Specification",
            "provider": "JSON-RPC",
            "url": "https://www.jsonrpc.org/specification",
        }
        other_ref["content_hash"] = _common.content_hash(other_ref)

        rule_export = self.make_export([rule], name="mixed-rule.yaml")
        meas_export = self.make_export(
            [measurement], kind="hardware-measurements", name="mixed-meas.yaml"
        )
        ref_export = self.make_export(
            [other_ref], kind="sourced-references", name="mixed-ref.yaml"
        )
        plan = self.plan([rule_export, meas_export, ref_export])
        self.assertEqual(["new", "new", "new"], [item.action for item in plan.items], [item.reason for item in plan.items])
        self.assertTrue(all(not item.related for item in plan.items))

        _, _, written = self.propose_apply([rule_export, meas_export, ref_export])
        self.assertTrue(written)
        corpus = self.corpus()
        self.assertEqual("rule", _common.body_key(corpus.index[rule["uuid"]].entry))
        self.assertEqual("measurement", _common.body_key(corpus.index[measurement["uuid"]].entry))
        self.assertEqual("reference", _common.body_key(corpus.index[other_ref["uuid"]].entry))
        self.assertEqual("reference", _common.body_key(corpus.index[packaged_ref["uuid"]].entry))
        replay = self.plan([rule_export, meas_export, ref_export])
        self.assertEqual(["no-op", "no-op", "no-op"], [item.action for item in replay.items])


class PlanCli(synctest.SyncTestCase):
    def _run(self, *extra, tools=synctest.TOOLS_PASS):
        cmd = [sys.executable, str(synctest.SYNC_DIR / "plan.py"), "--export", str(synctest.EXPORT_FIXTURE),
               "--corpus", str(self.corpus_dir), "--tools-dir", str(tools), "--today", "2026-09-09", *extra]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_dry_run_changes_nothing_and_reports_each_entry(self):
        before = synctest.dir_digest(self.corpus_dir)
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir))
        self.assertIn("no-op", proc.stdout)
        self.assertIn("new", proc.stdout)
        self.assertIn(synctest.UUID_EXPORT_NEW, proc.stdout)
        self.assertNotIn(str(self.tmp), proc.stdout, "no machine-specific paths in the plan")

    def test_json_output(self):
        proc = self._run("--json")
        self.assertEqual(0, proc.returncode, proc.stderr)
        data = synctest.yaml.safe_load(proc.stdout)
        self.assertEqual({"no-op": 1, "new": 1, "revision": 0, "duplicate-candidate": 0, "conflict": 0}, data["counts"])
        self.assertEqual(["passed", "passed"], [g["status"] for g in data["gates"]])

    def test_missing_gate_tools_fail_closed(self):
        proc = self._run(tools=synctest.TOOLS_MISSING)
        self.assertEqual(_common.EXIT_GATE, proc.returncode)
        self.assertIn("gate unavailable", proc.stdout + proc.stderr)

    def test_skip_gates_is_explicit(self):
        proc = self._run("--skip-gates", tools=synctest.TOOLS_MISSING)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("skipped", proc.stdout)


if __name__ == "__main__":
    unittest.main()
