"""Behaviour of conformance/runner.py against implementations it knows nothing about.

The runner is the part of the kit a fork actually runs, and the only thing it
is allowed to know about an implementation is a command line. So every case
here drives it exactly as a fork would - through a subprocess, against a stub
implementation in tests/fixtures/conformance/ - and checks the two properties
that make the runner trustworthy:

  * it reports per vector, not per run, so a near-miss implementation learns
    which behaviour it got wrong rather than that "conformance failed";
  * it fails loudly for every way an implementation can be wrong, including
    the ways that are not a wrong hash: no output, chatty output, a crash, a
    gate that accepts everything, a gate that refuses everything, an exporter
    that is not idempotent.

tests/fixtures/conformance/impl_independent.py is a second implementation of
the canonicalization written from docs/federation.md rather than from
conformance/reference.py. Its passing is the strongest evidence in this
repository that the vectors are reproducible from the specification instead of
merely from the code that produced them.
"""

import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNNER = REPO / "conformance" / "runner.py"
REFERENCE = REPO / "conformance" / "reference.py"
FIXTURES = REPO / "tests" / "fixtures" / "conformance"

try:
    import yaml  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required by the conformance kit: {exc}") from exc

PY = sys.executable


def run_runner(*args, timeout=180):
    return subprocess.run(
        [PY, str(RUNNER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        cwd=REPO,
    )


def impl(name, *extra):
    return " ".join([PY, str(FIXTURES / name), *extra])


def status_of(output, vector_id):
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == vector_id and parts[0] in ("PASS", "FAIL", "SKIP"):
            return parts[0]
    return None


class HashVectors(unittest.TestCase):
    def test_reference_implementation_passes_every_vector(self):
        result = run_runner("--hash-cmd", f"{PY} {REFERENCE}")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("conformance PASSED", result.stdout)

    def test_second_independent_implementation_passes_every_vector(self):
        result = run_runner("--hash-cmd", impl("impl_independent.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)

    def test_json_input_format_is_accepted(self):
        result = run_runner(
            "--hash-cmd",
            impl("impl_independent.py"),
            "--input-format",
            "entry-json",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_wrong_hash_fails_every_vector(self):
        result = run_runner("--hash-cmd", impl("impl_wrong_hash.py"))
        self.assertEqual(1, result.returncode)
        self.assertIn("conformance FAILED", result.stdout)
        self.assertNotIn("PASS", result.stdout)

    def test_a_near_miss_fails_only_the_vectors_it_gets_wrong(self):
        # impl_no_fingerprint_norm skips canonicalization step 2 and is
        # otherwise correct: the point of per-vector reporting is that it
        # learns exactly that.
        result = run_runner(
            "--hash-cmd",
            impl("impl_no_fingerprint_norm.py"),
            "--payload-cmd",
            impl("impl_no_fingerprint_norm.py", "--payload"),
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual("FAIL", status_of(result.stdout, "fingerprints-normalization"))
        self.assertEqual("FAIL", status_of(result.stdout, "fingerprints-all-empty"))
        self.assertEqual("PASS", status_of(result.stdout, "anchor-valid-entry"))
        self.assertEqual("PASS", status_of(result.stdout, "rule-optional-keys-absent"))
        self.assertEqual("PASS", status_of(result.stdout, "line-endings-lone-cr"))

    def test_payload_command_locates_the_divergence(self):
        result = run_runner(
            "--hash-cmd",
            impl("impl_no_fingerprint_norm.py"),
            "--payload-cmd",
            impl("impl_no_fingerprint_norm.py", "--payload"),
            "--only",
            "fingerprints-normalization",
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("first difference at character", result.stdout)
        self.assertIn("expected ...", result.stdout)

    def test_without_a_payload_command_the_report_says_how_to_get_a_diff(self):
        result = run_runner(
            "--hash-cmd", impl("impl_wrong_hash.py"), "--only", "anchor-valid-entry"
        )
        self.assertIn("--payload-cmd", result.stdout)

    def test_output_that_is_not_a_content_hash_is_a_failure(self):
        result = run_runner("--hash-cmd", impl("impl_malformed_output.py"))
        self.assertEqual(1, result.returncode)
        self.assertIn("is not a content_hash", result.stdout)

    def test_a_crashing_implementation_is_a_failure_not_a_traceback(self):
        result = run_runner("--hash-cmd", impl("impl_crashes.py"))
        self.assertEqual(1, result.returncode)
        self.assertIn("command exited 3", result.stdout)
        self.assertIn("cannot import the version module", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_a_command_that_does_not_exist_is_a_failure(self):
        result = run_runner("--hash-cmd", "definitely-not-a-real-command-xyz")
        self.assertEqual(1, result.returncode)
        self.assertIn("FAIL", result.stdout)


class GateVectors(unittest.TestCase):
    def test_a_plausible_redaction_gate_passes_the_redaction_vectors(self):
        result = run_runner("--redaction-cmd", impl("gate_redaction_regex.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("PASS", status_of(result.stdout, "redaction-ipv4"))
        self.assertEqual("PASS", status_of(result.stdout, "redaction-credential"))
        self.assertEqual("PASS", status_of(result.stdout, "redaction-clean-control"))

    def test_a_gate_that_accepts_everything_fails_the_rejection_vectors(self):
        result = run_runner("--redaction-cmd", impl("gate_accept_all.py"))
        self.assertEqual(1, result.returncode)
        self.assertEqual("FAIL", status_of(result.stdout, "redaction-ipv4"))
        self.assertEqual("PASS", status_of(result.stdout, "redaction-clean-control"))
        self.assertIn("expected verdict reject, got accept", result.stdout)

    def test_a_gate_that_refuses_everything_fails_the_accept_control(self):
        result = run_runner("--redaction-cmd", impl("gate_reject_all.py"))
        self.assertEqual(1, result.returncode)
        self.assertEqual("FAIL", status_of(result.stdout, "redaction-clean-control"))
        self.assertIn("expected verdict accept, got reject", result.stdout)

    def test_a_stable_exporter_passes_the_idempotence_vectors(self):
        result = run_runner("--export-cmd", impl("export_stable.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "PASS", status_of(result.stdout, "export-idempotent-unchanged-entry")
        )

    def test_an_exporter_that_stamps_its_output_fails_idempotence(self):
        result = run_runner("--export-cmd", impl("export_unstable.py"))
        self.assertEqual(1, result.returncode)
        self.assertIn("two exports of the same document differ", result.stdout)

    def test_the_schema_gate_vectors_run_against_the_real_schema(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest(
                "jsonschema is not installed; install it with "
                "python3 -m pip install jsonschema to run the schema gate vectors"
            )
        if not (REPO / "schemas" / "knowledge-v2.schema.json").is_file():
            self.skipTest("schemas/knowledge-v2.schema.json is not in this checkout")
        result = run_runner("--schema-cmd", impl("gate_schema_jsonschema.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "PASS", status_of(result.stdout, "schema-omitted-scope-dimension")
        )
        self.assertEqual("PASS", status_of(result.stdout, "schema-valid-control"))


class RunnerContract(unittest.TestCase):
    def test_no_implementation_command_is_a_usage_error(self):
        result = run_runner()
        self.assertEqual(2, result.returncode)
        self.assertIn("--hash-cmd", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_list_prints_the_whole_inventory(self):
        result = run_runner("--list")
        self.assertEqual(0, result.returncode)
        self.assertIn("anchor-valid-entry", result.stdout)
        self.assertIn("redaction-credential", result.stdout)
        self.assertIn("[group: anchor-scope-rule]", result.stdout)

    def test_skipping_a_gate_is_not_passing_it(self):
        result = run_runner("--hash-cmd", f"{PY} {REFERENCE}")
        self.assertEqual(0, result.returncode)
        self.assertIn("SKIP  schema-valid-control", result.stdout)
        self.assertIn("skipped", result.stdout)

    def test_running_nothing_at_all_is_a_failure(self):
        result = run_runner(
            "--hash-cmd", f"{PY} {REFERENCE}", "--only", "no-such-vector-id"
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("nothing was actually run", result.stdout)

    def test_a_missing_vector_directory_is_reported_not_raised(self):
        result = run_runner(
            "--hash-cmd", f"{PY} {REFERENCE}", "--vectors", "/nonexistent/vectors"
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("no such vector directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
