"""tools/validate.py — schema conformance plus the cross-entry rules."""

from __future__ import annotations

import unittest
from pathlib import Path

from test_tools_support import (
    EXAMPLE_ENTRY,
    FIXTURES,
    REPO_ROOT,
    TempDir,
    run_tool,
    valid_document,
    write_yaml,
)

from vaws_knowledge import validate

INVALID = FIXTURES / "invalid"


def problems_for(*paths: Path) -> list[str]:
    result = validate.validate_paths([str(p) for p in paths])
    return [p.render() for p in result.problems]


class PositiveTests(unittest.TestCase):
    def test_example_entry_is_valid(self):
        self.assertEqual(problems_for(EXAMPLE_ENTRY), [])

    def test_valid_fixture_is_valid(self):
        self.assertEqual(problems_for(FIXTURES / "valid"), [])

    def test_cli_exit_zero_on_valid_and_reports_counts(self):
        proc = run_tool("validate", str(FIXTURES / "valid"), str(REPO_ROOT / "corpus"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK", proc.stderr)

    def test_empty_corpus_directories_are_fine(self):
        # corpus/ ships empty on purpose; .gitkeep files are not corpus files.
        self.assertEqual(problems_for(REPO_ROOT / "corpus"), [])


class NegativeFixtureTests(unittest.TestCase):
    def assert_single_problem(self, path: Path, *needles: str) -> str:
        problems = problems_for(path)
        self.assertEqual(len(problems), 1, problems)
        for n in needles:
            self.assertIn(n, problems[0])
        return problems[0]

    def test_missing_scope_dimension(self):
        self.assert_single_problem(
            INVALID / "missing-scope-dimension.yaml",
            "entries[0].scope", "missing scope dimension 'cann'", "all twelve dimensions are required",
        )

    def test_any_without_basis(self):
        self.assert_single_problem(
            INVALID / "any-without-basis.yaml",
            "entries[0].scope.soc", "'any: true' requires a 'basis'",
        )

    def test_prose_as_evidence(self):
        self.assert_single_problem(
            INVALID / "prose-as-evidence.yaml",
            "verification.evidence[0].ref", "not prose",
        )

    def test_verified_without_verified_by(self):
        self.assert_single_problem(
            INVALID / "verified-without-verified-by.yaml",
            "entries[0].verification.verified_by", "at least one confirming handle",
        )

    def test_verified_by_submitter_only_and_bot(self):
        problems = problems_for(INVALID / "verified-by-submitter-and-bot.yaml")
        self.assertEqual(len(problems), 2, problems)
        joined = "\n".join(problems)
        self.assertIn("bot identity", joined)
        self.assertIn("vaws-review[bot]", joined)
        self.assertIn("only the submitter", joined)
        self.assertIn("'example-submitter'", joined)

    def test_hash_mismatch(self):
        msg = self.assert_single_problem(
            INVALID / "hash-mismatch.yaml",
            ".content_hash", "declared sha256:", "hashes to sha256:", "regenerate",
        )
        self.assertIn("81d274a4-f390-4c11-8475-608192a3b425", msg)

    def test_uuid_collision_across_files(self):
        first = INVALID / "uuid-collision" / "first.yaml"
        second = INVALID / "uuid-collision" / "second.yaml"
        # Each file alone is fine.
        self.assertEqual(problems_for(first), [])
        self.assertEqual(problems_for(second), [])
        problems = problems_for(first, second)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("second.yaml", problems[0])
        self.assertIn("duplicate uuid", problems[0])
        self.assertIn("first.yaml", problems[0])

    def test_layer_directory_mismatch(self):
        msg = self.assert_single_problem(
            FIXTURES / "layer-mismatch",
            "layer", "corpus/verified/", "declares layer: unverified",
        )
        self.assertIn("corpus/verified/known-failure-signatures.yaml", msg)

    def test_layer_check_needs_the_corpus_directory(self):
        # The same document outside corpus/<layer>/ is not a layer error.
        doc = valid_document()
        doc["layer"] = "unverified"
        with TempDir() as tmp:
            path = write_yaml(tmp / "somewhere" / "x.yaml", doc)
            self.assertEqual(problems_for(path), [])
            path = write_yaml(tmp / "corpus" / "unverified" / "x.yaml", doc)
            self.assertEqual(problems_for(path), [])
            path = write_yaml(tmp / "corpus" / "verified" / "x.yaml", doc)
            self.assertEqual(len(problems_for(path)), 1)

    def test_unverified_status_in_verified_layer(self):
        self.assert_single_problem(
            INVALID / "unverified-status-in-verified-layer.yaml",
            ".status", "layer: verified document cannot hold status: unverified",
        )

    def test_float_version_is_named_explicitly(self):
        msg = self.assert_single_problem(
            INVALID / "float-version.yaml",
            "entries[0].scope.torch.range.min", "YAML parsed this value as a number (2.5)", "Quote it", "min: '2.5'",
        )
        # And the confusing raw schema message is not emitted alongside it.
        self.assertNotIn("is not of type", msg)


class NegativeSyntheticTests(unittest.TestCase):
    """Variants built from the valid fixture in a temp dir."""

    def problems_after(self, mutate) -> list[str]:
        doc = valid_document()
        mutate(doc)
        with TempDir() as tmp:
            return problems_for(write_yaml(tmp / "doc.yaml", doc))

    def test_undeclared_field_is_reported_as_whitelist_violation(self):
        def m(doc):
            doc["entries"][0]["machine"] = "something"
        problems = self.problems_after(m)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("undeclared field(s) ['machine']", problems[0])
        self.assertIn("egress whitelist", problems[0])

    def test_evidence_as_bare_string_is_rejected(self):
        def m(doc):
            doc["entries"][0]["verification"]["evidence"] = ["worked for me on the shared box"]
        problems = self.problems_after(m)
        self.assertTrue(any("evidence[0]" in p and "not of type 'object'" in p for p in problems), problems)

    def test_float_in_values_and_verified_against(self):
        def m(doc):
            e = doc["entries"][0]
            e["scope"]["topology"] = {"values": [3.11]}
            e["verification"]["verified_against"]["torch"] = 2.5
        problems = self.problems_after(m)
        floats = [p for p in problems if "parsed this value as a number" in p]
        self.assertEqual(len(floats), 2, problems)
        self.assertTrue(any("scope.topology.values[0]" in p for p in floats))
        self.assertTrue(any("verified_against.torch" in p for p in floats))

    def test_submitter_only_is_rejected_in_verified_layer_regardless_of_status(self):
        def m(doc):
            e = doc["entries"][0]
            e["status"] = "resolved"
            e["lifecycle"]["resolved_by"] = {"type": "commit", "ref": "abcdef1234"}
            e["provenance"]["contributor"] = "example-reviewer"  # == the only verifier
        problems = self.problems_after(m)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("only the submitter", problems[0])
        self.assertIn("layer 'verified'", problems[0])

    def test_high_confidence_requires_established_status(self):
        def m(doc):
            e = doc["entries"][0]
            e["status"] = "unverified"
            doc["layer"] = "unverified"
        problems = self.problems_after(m)
        self.assertTrue(any("confidence 'high' is only allowed" in p for p in problems), problems)

    def test_resolved_requires_resolved_by(self):
        def m(doc):
            doc["entries"][0]["status"] = "resolved"
        problems = self.problems_after(m)
        self.assertTrue(any("resolved_by" in p for p in problems), problems)

    def test_self_reference_and_date_order(self):
        def m(doc):
            e = doc["entries"][0]
            e["lifecycle"]["superseded_by"] = e["uuid"]
            e["lifecycle"]["first_seen"] = "2030-01-01"
        problems = self.problems_after(m)
        self.assertTrue(any("supersede itself" in p for p in problems), problems)
        self.assertTrue(any("first_seen 2030-01-01 is after updated_at" in p for p in problems), problems)

    def test_non_mapping_document(self):
        with TempDir() as tmp:
            path = tmp / "list.yaml"
            path.write_text("- just\n- a list\n", encoding="utf-8")
            problems = problems_for(path)
        self.assertEqual(len(problems), 1)
        self.assertIn("document must be a mapping", problems[0])

    def test_cli_exit_code_and_message_shape(self):
        proc = run_tool("validate", str(INVALID / "missing-scope-dimension.yaml"))
        self.assertEqual(proc.returncode, 1)
        line = proc.stdout.strip().splitlines()[0]
        # file: path: message
        self.assertRegex(line, r"^tests/fixtures/tools/invalid/missing-scope-dimension\.yaml: entries\[0\]\.scope: ")
        self.assertIn("FAILED", proc.stderr)

    def test_missing_path_is_usage_error(self):
        proc = run_tool("validate", "does-not-exist.yaml")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no such file", proc.stderr)


if __name__ == "__main__":
    unittest.main()
