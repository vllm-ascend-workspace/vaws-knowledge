"""The write path, and the layers it must refuse.

Capture is the only way this service writes anything, and it may only ever
write the candidate layer. The refusal tests are the point: a capture call
that silently landed in `shared` or `project` would let an unreviewed single
observation impersonate a reviewed fact.

The canonicalization test is the other load-bearing one: `content_hash` is the
federated revision key, so this implementation is checked against the agreed
fixture in examples/valid-entry.yaml rather than against itself.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.server.capture import (  # noqa: E402
    CaptureRefused,
    CaptureRejected,
    builtin_content_hash,
    canonical_payload,
    capture,
)
from vaws_knowledge.server.layers import yaml  # noqa: E402
from vaws_knowledge.server.query import query  # noqa: E402

TODAY = dt.date(2026, 9, 7)

SCOPE = {
    "soc": {"values": ["ExampleSoC-A"]},
    "cann": {"values": ["0.0.EXAMPLE"]},
    "driver": {"values": ["0.0.example"]},
    "python_abi": {"any": True, "basis": "The failing path is pure Python text parsing."},
    "torch": {"range": {"min": "2.5.0", "max": None}},
    "torch_npu": {"any": True, "basis": "The failing call is not overridden by torch_npu."},
    "vllm": {"range": {"min": "0.9.0", "max": None}},
    "vllm_ascend": {"any": True, "basis": "Observed across several plugin revisions."},
    "model": {"any": True, "basis": "Occurs before any weights are read."},
    "topology": {"values": ["tp8"]},
    "execution_mode": {
        "any": True,
        "basis": "The failing step runs before an execution mode is selected.",
    },
    "component": {"values": ["service-bootstrap"]},
}

RULE = {
    "summary": "Example capture-path failure recorded from a local fix",
    "symptom": "The example service exits during startup with an example bootstrap error.",
    "root_cause": "The example bootstrap step runs before the mapping it depends on exists.",
    "resolution": "Order the mapping step before the bootstrap step.",
    "fingerprints": ["  EXAMPLE   Bootstrap Ordering  ", "example bootstrap ordering", ""],
}


def draft(**overrides):
    entry = {
        "slug": "example-captured-bootstrap-ordering",
        "scope": copy.deepcopy(SCOPE),
        "rule": copy.deepcopy(RULE),
    }
    entry.update(overrides)
    return entry


class RefusesEveryNonCandidateLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = support.build_config(candidate=self.tmp.name)

    def test_refuses_shared_and_project(self):
        for layer in ("shared", "project"):
            with self.subTest(layer=layer):
                with self.assertRaises(CaptureRefused) as ctx:
                    capture(draft(), layer=layer, config=self.config, today=TODAY)
                self.assertEqual(layer, ctx.exception.layer)
                self.assertIn("review-gated", str(ctx.exception))
        self.assertEqual([], list(pathlib.Path(self.tmp.name).iterdir()))

    def test_refuses_corpus_zone_names_and_anything_else(self):
        for layer in ("verified", "unverified", "corpus", "", "CANDIDATE", "candidate/../shared"):
            with self.subTest(layer=layer):
                with self.assertRaises(CaptureRefused):
                    capture(draft(), layer=layer, config=self.config, today=TODAY)
        self.assertEqual([], list(pathlib.Path(self.tmp.name).iterdir()))

    def test_refuses_when_the_candidate_mount_is_read_only(self):
        from vaws_knowledge.server.layers import load_config

        config = load_config(
            {"layers": {"candidate": {"root": self.tmp.name, "read_only": True}}},
            env={},
            base_dir=support.FIXTURES,
        )
        with self.assertRaises(CaptureRefused) as ctx:
            capture(draft(), config=config, today=TODAY)
        self.assertIn("read_only", str(ctx.exception))

    def test_refuses_when_no_candidate_root_is_configured(self):
        from vaws_knowledge.server.layers import load_config

        config = load_config(
            {"layers": {"candidate": {"enabled": False}}}, env={}, base_dir=support.FIXTURES
        )
        with self.assertRaises(CaptureRefused) as ctx:
            capture(draft(), config=config, today=TODAY)
        self.assertIn("VAWS_KNOWLEDGE_CANDIDATE_ROOT", str(ctx.exception))


class WritesCandidateEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.config = support.build_config(
            candidate=self.root,
            identity={
                "contributor": "example-handle",
                "origin_repo": "example-org/example-business-repo",
                "redaction_profile": "r1",
            },
        )

    def _capture(self, entry=None, **kwargs):
        return capture(entry or draft(), config=self.config, today=TODAY, **kwargs)

    def test_stamps_provenance_lifecycle_and_content_hash(self):
        result = self._capture()
        self.assertTrue(result["ok"])
        self.assertTrue(result["written"])
        self.assertEqual("candidate", result["layer"])
        self.assertEqual("created", result["action"])
        self.assertEqual("unverified", result["status"])

        document = yaml.safe_load(pathlib.Path(result["file"]).read_text())
        self.assertEqual(2, document["schema_version"])
        # schema v2 only knows the corpus review zones; the trust layer comes
        # from the mount, so a candidate capture is written as unverified.
        self.assertEqual("unverified", document["layer"])
        entry = document["entries"][0]
        self.assertEqual("example-handle", entry["provenance"]["contributor"])
        self.assertEqual(
            "example-org/example-business-repo", entry["provenance"]["origin_repo"]
        )
        self.assertEqual("2026-09-07", entry["provenance"]["submitted_at"])
        self.assertEqual("r1", entry["provenance"]["redaction_profile"])
        self.assertEqual("2026-09-07", entry["lifecycle"]["first_seen"])
        self.assertEqual(builtin_content_hash(entry), entry["content_hash"])
        self.assertRegex(entry["uuid"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

    def test_reports_which_canonicalization_produced_the_hash(self):
        result = self._capture()
        self.assertEqual(result["content_hash_source"], "vaws_knowledge.canonical")

    def test_a_supplied_content_hash_is_recomputed_and_the_override_reported(self):
        result = self._capture(draft(content_hash="sha256:" + "0" * 64))
        self.assertNotEqual("sha256:" + "0" * 64, result["content_hash"])
        self.assertTrue(any("did not match the canonicalization" in w for w in result["warnings"]))

    def test_same_uuid_same_hash_is_a_no_op(self):
        first = self._capture()
        again = self._capture(draft(uuid=first["uuid"]))
        self.assertEqual("unchanged", again["action"])
        self.assertFalse(again["written"])

    def test_same_uuid_changed_rule_is_a_revision(self):
        first = self._capture()
        entry = draft(uuid=first["uuid"])
        entry["rule"]["resolution"] = "Order the mapping step earlier, and assert it at startup."
        revised = self._capture(entry)
        self.assertEqual("revised", revised["action"])
        self.assertNotEqual(first["content_hash"], revised["content_hash"])
        document = yaml.safe_load(pathlib.Path(revised["file"]).read_text())
        self.assertEqual(1, len(document["entries"]))

    def test_dry_run_writes_nothing(self):
        result = self._capture(dry_run=True)
        self.assertFalse(result["written"])
        self.assertEqual([], list(self.root.iterdir()))

    def test_captured_entry_is_retrievable_and_labelled_candidate(self):
        result = self._capture()
        payload = query(
            self.config,
            text="example bootstrap ordering",
            reader_coordinate=support.READER_SOC_A,
            include_unverified=True,
            today=TODAY,
        ).to_dict()
        found = support.by_uuid(payload, result["uuid"])
        self.assertEqual("candidate", found["layer"])
        self.assertEqual("unverified", found["status"])
        self.assertTrue(found["applicability"]["applies"])

    def test_non_default_status_is_allowed_but_flagged_as_local_only(self):
        entry = draft(
            status="verified",
            confidence="high",
            verification={
                "evidence": [{"type": "run_manifest", "ref": "example-run-0100"}],
                "verified_by": ["example-other-handle"],
                "verified_against": {
                    "soc": "ExampleSoC-A",
                    "cann": "0.0.EXAMPLE",
                    "driver": "0.0.example",
                    "torch": "2.5.1",
                    "torch_npu": "2.5.1.example",
                    "vllm": "0.9.1",
                    "vllm_ascend": "0.0.0+example",
                },
                "last_verified_at": "2026-09-07",
            },
        )
        result = self._capture(entry)
        self.assertTrue(any("candidate layer is a local claim only" in w for w in result["warnings"]))

    def test_existing_unparsable_candidate_file_is_not_clobbered(self):
        path = self.root / "known-failure-signatures.yaml"
        path.write_text("entries: [ unterminated\n")
        with self.assertRaises(CaptureRefused) as ctx:
            self._capture()
        self.assertIn("refusing to overwrite", str(ctx.exception))
        self.assertEqual("entries: [ unterminated\n", path.read_text())


class RejectsEntriesTheCorpusWouldReject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = support.build_config(candidate=self.tmp.name)

    def _reject(self, entry):
        with self.assertRaises(CaptureRejected) as ctx:
            capture(entry, config=self.config, today=TODAY)
        self.assertEqual([], list(pathlib.Path(self.tmp.name).iterdir()))
        return ctx.exception.problems

    def test_missing_scope_dimension(self):
        entry = draft()
        entry["scope"].pop("driver")
        problems = self._reject(entry)
        self.assertTrue(any("driver" in p for p in problems))

    def test_independence_claim_without_a_basis(self):
        entry = draft()
        entry["scope"]["soc"] = {"any": True}
        self.assertTrue(any("basis" in p for p in self._reject(entry)))

    def test_stub_basis(self):
        entry = draft()
        entry["scope"]["soc"] = {"any": True, "basis": "dunno"}
        self.assertTrue(any("12 characters" in p for p in self._reject(entry)))

    def test_constraint_that_is_both_bounded_and_independent(self):
        entry = draft()
        entry["scope"]["topology"] = {"values": ["tp8"], "any": True, "basis": "x" * 20}
        self.assertTrue(any("exactly one of" in p for p in self._reject(entry)))

    def test_range_missing_a_bound(self):
        entry = draft()
        entry["scope"]["torch"] = {"range": {"min": "2.5.0"}}
        self.assertTrue(any("explicit null" in p for p in self._reject(entry)))

    def test_float_version_bound(self):
        entry = draft()
        entry["scope"]["torch"] = {"range": {"min": 2.5, "max": None}}
        self.assertTrue(any("float" in p for p in self._reject(entry)))

    def test_undeclared_entry_field(self):
        entry = draft(internal_note="leaked field")
        self.assertTrue(any("egress whitelist" in p for p in self._reject(entry)))

    def test_undeclared_scope_dimension(self):
        entry = draft()
        entry["scope"]["phase_of_moon"] = {"any": True, "basis": "x" * 20}
        self.assertTrue(any("phase_of_moon" in p for p in self._reject(entry)))

    def test_symptom_without_a_root_cause(self):
        entry = draft()
        entry["rule"]["root_cause"] = "   "
        self.assertTrue(any("root_cause" in p for p in self._reject(entry)))

    def test_high_confidence_on_an_unverified_claim(self):
        entry = draft(confidence="high")
        self.assertTrue(any("writing" in p and "not evidence" in p for p in self._reject(entry)))

    def test_verified_status_without_evidence(self):
        entry = draft(status="verified", confidence="medium")
        problems = self._reject(entry)
        self.assertTrue(any("verification record" in p for p in problems))

    def test_prose_in_place_of_an_evidence_reference(self):
        entry = draft(
            status="verified",
            confidence="medium",
            verification={
                "evidence": ["it worked when I tried it"],
                "verified_by": ["example-other-handle"],
                "verified_against": {
                    "soc": "ExampleSoC-A",
                    "cann": "0.0.EXAMPLE",
                    "driver": "0.0.example",
                    "torch": "2.5.1",
                    "torch_npu": "2.5.1.example",
                    "vllm": "0.9.1",
                    "vllm_ascend": "0.0.0+example",
                },
                "last_verified_at": "2026-09-07",
            },
        )
        self.assertTrue(any("references, not prose" in p for p in self._reject(entry)))

    def test_bad_slug(self):
        entry = draft(slug="Not A Slug")
        self.assertTrue(any("slug" in p for p in self._reject(entry)))

    def test_numeric_range_bound_is_rejected_before_a_hash_is_published(self):
        entry = draft()
        entry["scope"]["torch"]["range"]["min"] = 2.5
        problems = self._reject(entry)
        self.assertTrue(any("2.5" in p and "stringify" in p for p in problems), problems)

    def test_non_string_fingerprint_is_rejected_not_stringified(self):
        entry = draft()
        entry["rule"]["fingerprints"] = [123]
        problems = self._reject(entry)
        self.assertTrue(any("fingerprint" in p and "stringify" in p for p in problems), problems)


class Canonicalization(unittest.TestCase):
    """docs/federation.md, checked against the agreed reference fixture."""

    def setUp(self):
        if yaml is None:  # pragma: no cover
            self.skipTest("PyYAML is required to read the reference fixture")
        path = support.REPO / "examples" / "valid-entry.yaml"
        self.reference = yaml.safe_load(path.read_text())["entries"][0]

    def test_reference_entry_hash_reproduces(self):
        self.assertEqual(self.reference["content_hash"], builtin_content_hash(self.reference))

    def test_only_scope_and_rule_participate(self):
        mutated = copy.deepcopy(self.reference)
        mutated["status"] = "stale"
        mutated["confidence"] = "low"
        mutated["provenance"]["contributor"] = "someone-else"
        mutated["provenance"]["redaction_profile"] = "r19"
        mutated["redaction_cleared_under"] = "r99"
        mutated["lifecycle"]["updated_at"] = "2026-12-31"
        mutated["verification"]["last_verified_at"] = "2026-12-31"
        mutated["verification"]["verified_by"] = ["example-revalidator"]
        self.assertEqual(builtin_content_hash(self.reference), builtin_content_hash(mutated))

    def test_fingerprints_are_normalized_deduplicated_and_sorted(self):
        left = copy.deepcopy(self.reference)
        right = copy.deepcopy(self.reference)
        right["rule"]["fingerprints"] = [
            "  " + left["rule"]["fingerprints"][2].upper() + " ",
            left["rule"]["fingerprints"][0].replace(" ", "   "),
            "",
            left["rule"]["fingerprints"][1],
            left["rule"]["fingerprints"][1],
        ]
        self.assertEqual(builtin_content_hash(left), builtin_content_hash(right))

    def test_prose_whitespace_is_trimmed_but_not_reflowed(self):
        trimmed = copy.deepcopy(self.reference)
        trimmed["rule"]["summary"] = "  " + trimmed["rule"]["summary"] + "\n"
        self.assertEqual(builtin_content_hash(self.reference), builtin_content_hash(trimmed))

        reflowed = copy.deepcopy(self.reference)
        reflowed["rule"]["summary"] = reflowed["rule"]["summary"].replace(" ", "  ")
        self.assertNotEqual(builtin_content_hash(self.reference), builtin_content_hash(reflowed))

    def test_crlf_normalizes_to_lf(self):
        crlf = copy.deepcopy(self.reference)
        crlf["rule"]["root_cause"] = self.reference["rule"]["root_cause"].replace("\n", "\r\n")
        self.assertEqual(builtin_content_hash(self.reference), builtin_content_hash(crlf))

    def test_payload_is_sorted_compact_json_of_rule_and_scope_only(self):
        payload = canonical_payload(self.reference)
        self.assertTrue(payload.startswith('{"rule":{'))
        self.assertIn('"scope":{', payload)
        self.assertNotIn('": ', payload)  # separators are (",", ":"), no spaces
        self.assertEqual({"rule", "scope"}, set(json.loads(payload)))

    def test_nbsp_is_content_and_ascii_lower_is_ascii_only(self):
        mutated = copy.deepcopy(self.reference)
        mutated["rule"]["summary"] = "\u00a0" + mutated["rule"]["summary"] + "\u3000"
        mutated["rule"]["fingerprints"] = ["ABC İ É Σ"]
        payload = json.loads(canonical_payload(mutated))
        self.assertTrue(payload["rule"]["summary"].startswith("\u00a0"))
        self.assertEqual(["abc İ É Σ"], payload["rule"]["fingerprints"])

    def test_per_line_trailing_ascii_whitespace_is_stripped(self):
        mutated = copy.deepcopy(self.reference)
        mutated["rule"]["summary"] = "First line  \nSecond line"
        mutated["scope"]["soc"]["basis"] = "Synthetic first line\t \nsecond line"
        payload = json.loads(canonical_payload(mutated))
        self.assertEqual("First line\nSecond line", payload["rule"]["summary"])
        self.assertEqual("Synthetic first line\nsecond line", payload["scope"]["soc"]["basis"])

    def test_numeric_bound_is_rejected_not_stringified(self):
        mutated = copy.deepcopy(self.reference)
        mutated["scope"]["torch"]["range"]["min"] = 2.5
        with self.assertRaises(ValueError) as ctx:
            builtin_content_hash(mutated)
        self.assertIn("2.5", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
