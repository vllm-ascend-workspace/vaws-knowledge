"""tools/export.py — the egress gate."""

from __future__ import annotations

import copy
import unittest

import yaml

from test_tools_support import (
    FIXTURES,
    TempDir,
    calibration_sentence,
    load_yaml,
    run_tool,
    versionlike,
    write_yaml,
)

from tools import canonical, export, redact, validate
from tools._common import load_schema

CANDIDATE = FIXTURES / "export" / "candidate.yaml"
CANDIDATE_UNDECLARED = FIXTURES / "export" / "candidate-with-undeclared-fields.yaml"
ORIGIN = "example-org/vllm-ascend-workspace"


def export_ok(*args: str):
    proc = run_tool("export", *args)
    assert proc.returncode == 0, proc.stderr
    return proc


class WhitelistTests(unittest.TestCase):
    def test_undeclared_fields_are_found_by_schema_walk(self):
        entry = load_yaml(CANDIDATE_UNDECLARED)[0]
        paths = export.undeclared_fields(entry, load_schema())
        self.assertEqual(sorted(paths), [("local_notes",), ("scope", "soc", "observed_on")])

    def test_declared_fields_incl_conditional_branches_are_not_flagged(self):
        entry = load_yaml(FIXTURES / "valid" / "known-failure-signatures.yaml")["entries"][0]
        entry["lifecycle"]["resolved_by"] = {"type": "commit", "ref": "abcdef1"}
        entry["conflicts"] = [{"with": entry["uuid"], "undeclared_dimensions": ["driver"], "recorded_at": "2026-09-07"}]
        self.assertEqual(export.undeclared_fields(entry, load_schema()), [])

    def test_refuses_undeclared_by_default(self):
        proc = run_tool("export", str(CANDIDATE_UNDECLARED), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("undeclared field(s) scope.soc.observed_on, local_notes", proc.stderr)
        self.assertIn("--drop-undeclared", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_drop_undeclared_never_passes_them_through(self):
        proc = export_ok(str(CANDIDATE_UNDECLARED), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--drop-undeclared")
        self.assertIn("dropped undeclared field(s)", proc.stderr)
        self.assertNotIn("local_notes", proc.stdout)
        self.assertNotIn("observed_on", proc.stdout)
        self.assertNotIn("a private note", proc.stdout)
        doc = yaml.safe_load(proc.stdout)
        self.assertEqual(validate.validate_document(doc, "<out>")[0], [])


class RedactionGateTests(unittest.TestCase):
    def test_refuses_on_leak_in_rule_body(self):
        cand = load_yaml(CANDIDATE)
        cand[0]["rule"]["resolution"] += " " + calibration_sentence()
        with TempDir() as tmp:
            path = write_yaml(tmp / "cand.yaml", cand)
            proc = run_tool("export", str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("redaction (r2)", proc.stderr)
        self.assertIn("[ipv4-address]", proc.stderr)
        self.assertIn("entries[0].rule.resolution", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # The refusal message masks the value: a CI log must not become the leak.
        self.assertNotIn(calibration_sentence(), proc.stderr)

    def test_local_allowlist_can_clear_a_false_positive(self):
        cand = load_yaml(CANDIDATE)
        cand[0]["verification"]["verified_against"]["driver"] = versionlike("1")
        with TempDir() as tmp:
            path = write_yaml(tmp / "cand.yaml", cand)
            refused = run_tool("export", str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
            allowed = run_tool("export", str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--allow", versionlike("1"))
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)


class StampAndHashTests(unittest.TestCase):
    def test_provenance_and_hash_are_stamped(self):
        proc = export_ok(str(CANDIDATE), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--contributor", "example-contributor", "--submitted-at", "2026-09-07")
        doc = yaml.safe_load(proc.stdout)
        self.assertEqual(doc["schema_version"], 2)
        self.assertEqual(doc["layer"], "unverified")
        self.assertEqual(doc["kind"], "known-failure-signatures")
        entry = doc["entries"][0]
        self.assertEqual(entry["provenance"], {
            "contributor": "example-contributor",
            "origin_repo": ORIGIN,
            "submitted_at": "2026-09-07",
            "redaction_profile": redact.REDACTION_PROFILE,
        })
        self.assertEqual(entry["content_hash"], canonical.content_hash(entry))
        # Document date derives from the entries, not from the clock.
        self.assertEqual(doc["updated_at"], entry["lifecycle"]["updated_at"])

    def test_output_validates_and_is_redaction_clean(self):
        proc = export_ok(str(CANDIDATE), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        with TempDir() as tmp:
            out = tmp / "corpus" / "unverified" / "known-failure-signatures.yaml"
            out.parent.mkdir(parents=True)
            out.write_text(proc.stdout, encoding="utf-8")
            self.assertEqual(run_tool("validate", str(out)).returncode, 0)
            self.assertEqual(run_tool("redact", "--check", str(out)).returncode, 0)

    def test_kind_and_origin_required(self):
        proc = run_tool("export", str(CANDIDATE), "--origin-repo", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--kind", proc.stderr)
        proc = run_tool("export", str(CANDIDATE), "--kind", "known-failure-signatures")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--origin-repo", proc.stderr)

    def test_stale_hash_on_input_is_recomputed(self):
        cand = load_yaml(CANDIDATE)
        cand[0]["content_hash"] = "sha256:" + "0" * 64
        with TempDir() as tmp:
            path = write_yaml(tmp / "cand.yaml", cand)
            proc = export_ok(str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        self.assertIn("content_hash recomputed", proc.stderr)
        doc = yaml.safe_load(proc.stdout)
        self.assertEqual(doc["entries"][0]["content_hash"], canonical.content_hash(doc["entries"][0]))

    def test_invalid_candidate_is_refused_with_validator_messages(self):
        cand = load_yaml(CANDIDATE)
        del cand[0]["scope"]["driver"]
        with TempDir() as tmp:
            path = write_yaml(tmp / "cand.yaml", cand)
            proc = run_tool("export", str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("does not validate", proc.stderr)
        self.assertIn("missing scope dimension 'driver'", proc.stderr)

    def test_numeric_range_bound_is_refused_before_a_hash_is_published(self):
        cand = load_yaml(CANDIDATE)
        cand[0]["scope"]["torch"]["range"]["min"] = 2.5
        with TempDir() as tmp:
            path = write_yaml(tmp / "cand.yaml", cand)
            proc = run_tool("export", str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("2.5", proc.stderr)

    def test_non_string_fingerprint_is_refused_not_stringified(self):
        cand = load_yaml(CANDIDATE)
        cand[0]["rule"]["fingerprints"] = [123]
        with TempDir() as tmp:
            path = write_yaml(tmp / "cand.yaml", cand)
            proc = run_tool("export", str(path), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("123", proc.stderr)


class IdempotencyTests(unittest.TestCase):
    ARGS = ("--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--submitted-at", "2026-09-07")

    def test_reexport_is_byte_identical(self):
        with TempDir() as tmp:
            a, b = tmp / "a.yaml", tmp / "b.yaml"
            export_ok(str(CANDIDATE), *self.ARGS, "-o", str(a))
            export_ok(str(CANDIDATE), *self.ARGS, "-o", str(b))
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_exporting_an_export_is_a_fixed_point(self):
        with TempDir() as tmp:
            first, second = tmp / "first.yaml", tmp / "second.yaml"
            export_ok(str(CANDIDATE), *self.ARGS, "-o", str(first))
            # No flags this time: kind/origin/date all come from the document.
            export_ok(str(first), "-o", str(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_input_order_and_key_order_do_not_matter(self):
        cand = load_yaml(CANDIDATE)
        entry = cand[0]
        shuffled = {k: entry[k] for k in reversed(list(entry))}
        shuffled["scope"] = {k: entry["scope"][k] for k in reversed(list(entry["scope"]))}
        second = copy.deepcopy(entry)
        second["uuid"] = "00000000-0000-4000-8000-000000000001"
        second["slug"] = "fixture-second"
        with TempDir() as tmp:
            p1 = write_yaml(tmp / "one.yaml", [entry, second])
            p2 = write_yaml(tmp / "two.yaml", [second, shuffled])
            o1, o2 = tmp / "o1.yaml", tmp / "o2.yaml"
            export_ok(str(p1), *self.ARGS, "-o", str(o1))
            export_ok(str(p2), *self.ARGS, "-o", str(o2))
            self.assertEqual(o1.read_bytes(), o2.read_bytes())
            doc = yaml.safe_load(o1.read_text())
            self.assertEqual([e["uuid"] for e in doc["entries"]], sorted(e["uuid"] for e in doc["entries"]))

    def test_previous_export_carries_submitted_at_for_unchanged_revision(self):
        with TempDir() as tmp:
            first = tmp / "first.yaml"
            export_ok(str(CANDIDATE), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--submitted-at", "2026-01-01", "-o", str(first))
            # Same claim on a later date, with --previous: byte-identical.
            again = tmp / "again.yaml"
            export_ok(str(CANDIDATE), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--submitted-at", "2026-09-07", "--previous", str(first), "-o", str(again))
            self.assertEqual(first.read_bytes(), again.read_bytes())
            # A changed claim is a new revision: same uuid, new hash, new date.
            cand = load_yaml(CANDIDATE)
            cand[0]["rule"]["summary"] += " (revised)"
            cand[0]["lifecycle"]["updated_at"] = "2026-09-08"
            changed = write_yaml(tmp / "changed.yaml", cand)
            revised = tmp / "revised.yaml"
            export_ok(str(changed), "--kind", "known-failure-signatures", "--origin-repo", ORIGIN, "--submitted-at", "2026-09-08", "--previous", str(first), "-o", str(revised))
            d1 = yaml.safe_load(first.read_text())["entries"][0]
            d2 = yaml.safe_load(revised.read_text())["entries"][0]
            self.assertEqual(d1["uuid"], d2["uuid"])
            self.assertNotEqual(d1["content_hash"], d2["content_hash"])
            self.assertEqual(d2["provenance"]["submitted_at"], "2026-09-08")
            self.assertEqual(d2["verification"]["last_verified_at"], d1["verification"]["last_verified_at"])

    def test_revision_must_advance_updated_at(self):
        with TempDir() as tmp:
            first = tmp / "first.yaml"
            export_ok(str(CANDIDATE), *self.ARGS, "-o", str(first))
            cand = load_yaml(CANDIDATE)
            cand[0]["rule"]["summary"] += " (revised)"  # claim changed, date not
            changed = write_yaml(tmp / "changed.yaml", cand)
            proc = run_tool("export", str(changed), *self.ARGS, "--previous", str(first))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lifecycle.updated_at", proc.stderr)
        self.assertIn("did not advance", proc.stderr)

    def test_duplicate_uuid_across_inputs_is_refused(self):
        proc = run_tool("export", str(CANDIDATE), str(CANDIDATE), *self.ARGS)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("duplicate uuid", proc.stderr)


if __name__ == "__main__":
    unittest.main()
