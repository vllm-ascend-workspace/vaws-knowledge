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
import json
import os
import subprocess
import sys
import unittest

import vaws_knowledge.conformance as _conformance_pkg
from vaws_knowledge.conformance import runner as conformance_runner

REPO = pathlib.Path(__file__).resolve().parent.parent
KIT = pathlib.Path(_conformance_pkg.__file__).resolve().parent
REFERENCE = KIT / "reference.py"
FIXTURES = REPO / "tests" / "fixtures" / "conformance"

try:
    import yaml  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required by the conformance kit: {exc}") from exc

PY = sys.executable


def run_runner(*args, timeout=180):
    return subprocess.run(
        [PY, "-m", "vaws_knowledge", "conformance", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        cwd=REPO,
    )


def impl(name, *extra):
    parts = [PY, str(FIXTURES / name), *extra]
    return json.dumps(parts)


def status_of(output, vector_id):
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == vector_id and parts[0] in ("PASS", "FAIL", "SKIP"):
            return parts[0]
    return None


class HashVectors(unittest.TestCase):
    def test_reference_implementation_passes_every_vector(self):
        result = run_runner("--hash-cmd", json.dumps([PY, str(REFERENCE)]))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("conformance PASSED", result.stdout)

    def test_second_independent_implementation_passes_every_vector(self):
        result = run_runner("--hash-cmd", impl("impl_independent.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)

    def test_tools_adapter_passes_every_vector(self):
        result = run_runner("--hash-cmd", impl("impl_tools.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        source = (FIXTURES / "impl_tools.py").read_text()
        self.assertIn("from vaws_knowledge import canonical", source)
        self.assertNotIn("import reference", source)
        self.assertNotIn("from vaws_knowledge.conformance", source)

    def test_server_fallback_adapter_passes_every_vector(self):
        result = run_runner("--hash-cmd", impl("impl_server.py"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("PASS  non-ascii-whitespace-preserved", result.stdout)
        self.assertIn("SKIP  schema-valid-control", result.stdout)

    def test_sync_adapter_passes_every_vector(self):
        result = run_runner("--hash-cmd", impl("impl_sync.py"))
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
        self.assertEqual("FAIL", status_of(result.stdout, "ascii-lowercase-preserves-nonascii"))
        self.assertEqual("PASS", status_of(result.stdout, "anchor-valid-entry"))
        self.assertEqual("PASS", status_of(result.stdout, "rule-optional-keys-absent"))
        self.assertEqual("PASS", status_of(result.stdout, "line-endings-lone-cr"))
        self.assertEqual("PASS", status_of(result.stdout, "line-trailing-ascii-whitespace"))

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

    def test_a_gate_that_accepts_everything_fails_the_conflicts_rejection_vector(self):
        # The conflicts class is the first gate class whose vectors carry more
        # than one entry, so it is worth proving the runner drives it at all.
        result = run_runner("--conflicts-cmd", impl("gate_accept_all.py"))
        self.assertEqual(1, result.returncode)
        self.assertEqual(
            "FAIL", status_of(result.stdout, "conflicts-measurement-contradicting-value")
        )
        self.assertEqual(
            "PASS", status_of(result.stdout, "conflicts-theoretical-and-sustained-control")
        )

    def test_a_gate_that_refuses_everything_fails_the_conflicts_controls(self):
        result = run_runner("--conflicts-cmd", impl("gate_reject_all.py"))
        self.assertEqual(1, result.returncode)
        self.assertEqual(
            "FAIL", status_of(result.stdout, "conflicts-theoretical-and-sustained-control")
        )
        self.assertEqual(
            "FAIL",
            status_of(result.stdout, "conflicts-measurement-disjoint-coordinate-control"),
        )

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
        from vaws_knowledge._common import SCHEMA_PATH

        if not pathlib.Path(SCHEMA_PATH).is_file():
            self.skipTest("packaged knowledge-v2.schema.json is not available")
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
        self.assertIn("non-ascii-whitespace-preserved", result.stdout)
        self.assertIn("no-unicode-normalization", result.stdout)
        self.assertIn("redaction-credential", result.stdout)
        self.assertIn("[group: anchor-scope-rule]", result.stdout)

    def test_skipping_a_gate_is_not_passing_it(self):
        result = run_runner("--hash-cmd", json.dumps([PY, str(REFERENCE)]))
        self.assertEqual(0, result.returncode)
        self.assertIn("SKIP  schema-valid-control", result.stdout)
        self.assertIn("skipped", result.stdout)

    def test_running_nothing_at_all_is_a_failure(self):
        result = run_runner(
            "--hash-cmd", json.dumps([PY, str(REFERENCE)]), "--only", "no-such-vector-id"
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("nothing was actually run", result.stdout)

    def test_a_missing_vector_directory_is_reported_not_raised(self):
        result = run_runner(
            "--hash-cmd", json.dumps([PY, str(REFERENCE)]), "--vectors", "/nonexistent/vectors"
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("no such vector directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


REJECT_VECTOR = "schema-omitted-scope-dimension"
ACCEPT_VECTOR = "schema-valid-control"
HANG_SECONDS = 8.0
GATE_TIMEOUT = 0.4


def run_protocol(*gate_args, only=REJECT_VECTOR, gate_timeout=None, runner_timeout=60):
    args = ["--schema-cmd", impl("gate_protocol.py", *gate_args), "--only", only]
    if gate_timeout is not None:
        args.extend(["--timeout", str(gate_timeout)])
    return run_runner(*args, timeout=runner_timeout)


class GateVerdictContract(unittest.TestCase):
    """A startup or transport failure is not a semantic decision.

    These cases first failed on the original runner, which treated any
    nonzero exit as reject and empty exit-0 as accept. A negative vector
    must not PASS unless the gate completed with an explicit reject token.
    """

    def test_read_verdict_does_not_infer_from_exit_status(self):
        cases = [
            (127, b"", b"command not found"),
            (2, b"", b"usage error"),
            (1, b"", b"Traceback: import failure"),
            (0, b"", b""),
            (None, b"reject", b"timed out"),
        ]
        for code, out, err in cases:
            with self.subTest(code=code, out=out):
                self.assertIsNone(
                    conformance_runner.read_verdict(code, out, err),
                    f"read_verdict({code!r}, {out!r}, {err!r}) must not "
                    "invent a semantic verdict",
                )

    def test_read_verdict_accepts_explicit_tokens_with_legal_exits(self):
        self.assertEqual(
            "accept", conformance_runner.read_verdict(0, b"accept\n", b"")
        )
        self.assertEqual(
            "reject", conformance_runner.read_verdict(0, b"reject\n", b"")
        )
        self.assertEqual(
            "reject", conformance_runner.read_verdict(1, b"reject\n", b"")
        )
        self.assertEqual(
            "accept",
            conformance_runner.read_verdict(0, b"  accepted  \n", b""),
        )

    def test_read_verdict_rejects_malformed_and_contradictory_output(self):
        self.assertIsNone(
            conformance_runner.read_verdict(0, b"accept extra\n", b"")
        )
        self.assertIsNone(
            conformance_runner.read_verdict(0, b"accept\nreject\n", b"")
        )
        self.assertIsNone(
            conformance_runner.read_verdict(1, b"accept\n", b"")
        )
        self.assertIsNone(
            conformance_runner.read_verdict(2, b"reject\n", b"")
        )
        self.assertIsNone(
            conformance_runner.read_verdict(0, b"\xa0accept\n", b"")
        )

    def _assert_protocol_failure(self, result, vector_id=REJECT_VECTOR):
        combined = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, combined)
        self.assertEqual("FAIL", status_of(result.stdout, vector_id), combined)
        self.assertNotEqual("PASS", status_of(result.stdout, vector_id), combined)
        self.assertIn("execution/protocol failure", result.stdout)
        self.assertNotIn("1416a279-1215-4adf-a978-82b40a3be0bc", combined)

    def test_exit_only_zero_is_not_accept(self):
        result = run_protocol("--exit", "0", only=ACCEPT_VECTOR)
        self._assert_protocol_failure(result, ACCEPT_VECTOR)

    def test_exit_only_one_is_not_reject_on_a_negative_vector(self):
        result = run_protocol("--exit", "1", "--stderr", "usage error")
        self._assert_protocol_failure(result)
        self.assertIn("stderr:", result.stdout)

    def test_exit_only_two_is_not_reject_on_a_negative_vector(self):
        result = run_protocol("--exit", "2", "--stderr", "usage error")
        self._assert_protocol_failure(result)
        self.assertIn("exit 2", result.stdout)

    def test_exit_only_127_is_not_reject_on_a_negative_vector(self):
        result = run_protocol("--exit", "127")
        self._assert_protocol_failure(result)
        self.assertIn("127", result.stdout)

    def test_a_crashing_gate_does_not_pass_a_negative_vector(self):
        result = run_protocol("--raise")
        self._assert_protocol_failure(result)
        self.assertNotEqual("PASS", status_of(result.stdout, REJECT_VECTOR))

    def test_a_traceback_is_a_protocol_failure_not_a_reject(self):
        result = run_protocol("--raise")
        self._assert_protocol_failure(result)
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn("stderr:", result.stdout)

    def test_timeout_with_partial_reject_token_is_not_a_verdict(self):
        result = run_protocol(
            "--token",
            "reject",
            "--sleep",
            str(HANG_SECONDS),
            gate_timeout=GATE_TIMEOUT,
            runner_timeout=20,
        )
        self._assert_protocol_failure(result)
        self.assertIn("timed out", result.stdout)

    def test_timeout_without_a_token_is_not_a_verdict(self):
        result = run_protocol(
            "--sleep",
            str(HANG_SECONDS),
            gate_timeout=GATE_TIMEOUT,
            runner_timeout=20,
        )
        self._assert_protocol_failure(result)
        self.assertIn("timed out", result.stdout)

    def test_malformed_stdout_is_a_protocol_failure(self):
        prose = run_protocol(
            "--token", "accept", "--prose", "because schema", only=ACCEPT_VECTOR
        )
        self._assert_protocol_failure(prose, ACCEPT_VECTOR)
        unknown = run_protocol("--token", "not-a-verdict")
        self._assert_protocol_failure(unknown)

    def test_contradictory_lines_are_a_protocol_failure(self):
        result = run_protocol("--token", "accept", "--token", "reject")
        self._assert_protocol_failure(result)

    def test_accept_token_with_nonzero_exit_is_a_protocol_failure(self):
        result = run_protocol("--token", "accept", "--exit", "1", only=ACCEPT_VECTOR)
        self._assert_protocol_failure(result, ACCEPT_VECTOR)

    def test_explicit_reject_with_exit_zero_is_a_semantic_reject(self):
        result = run_protocol("--token", "reject", "--exit", "0")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("PASS", status_of(result.stdout, REJECT_VECTOR))

    def test_explicit_reject_with_exit_one_is_a_semantic_reject(self):
        result = run_protocol("--token", "reject", "--exit", "1")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("PASS", status_of(result.stdout, REJECT_VECTOR))

    def test_explicit_accept_with_exit_zero_is_a_semantic_accept(self):
        result = run_protocol("--token", "accept", "--exit", "0", only=ACCEPT_VECTOR)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("PASS", status_of(result.stdout, ACCEPT_VECTOR))

    def test_known_bad_document_with_a_real_reject_passes(self):
        result = run_protocol("--token", "reject", "--exit", "1")
        self.assertEqual("PASS", status_of(result.stdout, REJECT_VECTOR))
        self.assertNotIn("execution/protocol failure", result.stdout)

    def test_known_good_document_with_a_real_accept_passes(self):
        result = run_protocol("--token", "accept", "--exit", "0", only=ACCEPT_VECTOR)
        self.assertEqual("PASS", status_of(result.stdout, ACCEPT_VECTOR))

    def test_missing_command_does_not_pass_a_negative_vector(self):
        result = run_runner(
            "--schema-cmd",
            "definitely-not-a-real-command-xyz-gate",
            "--only",
            REJECT_VECTOR,
        )
        self._assert_protocol_failure(result)
        self.assertRegex(result.stdout, r"exit: 1\b" if os.name == "nt" else r"exit 12[67]")

    def test_signal_termination_is_a_protocol_failure(self):
        result = run_protocol("--signal", "15")
        self._assert_protocol_failure(result)
        self.assertTrue(
            "signal" in result.stdout or "exit 143" in result.stdout or
            (os.name == "nt" and "exit 15" in result.stdout),
            result.stdout,
        )

    def test_unconfigured_gate_class_remains_skip(self):
        result = run_runner("--redaction-cmd", impl("gate_accept_all.py"))
        self.assertEqual("SKIP", status_of(result.stdout, ACCEPT_VECTOR))
        self.assertEqual("SKIP", status_of(result.stdout, REJECT_VECTOR))
        self.assertNotEqual("PASS", status_of(result.stdout, ACCEPT_VECTOR))
        self.assertIn("skipped", result.stdout)

    def test_semantic_mismatch_is_not_labelled_a_protocol_failure(self):
        result = run_protocol("--token", "accept", "--exit", "0")
        self.assertEqual(1, result.returncode)
        self.assertEqual("FAIL", status_of(result.stdout, REJECT_VECTOR))
        self.assertIn("expected verdict reject, got accept", result.stdout)
        self.assertNotIn("execution/protocol failure", result.stdout)


class RealToolAdapter(unittest.TestCase):
    """The adapter recipe must invoke the real tools, not a fake verdict."""

    def test_schema_adapter_accepts_valid_and_rejects_invalid_documents(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest(
                "jsonschema is not installed; install it with "
                "python3 -m pip install jsonschema to run the schema adapter"
            )
        try:
            import vaws_knowledge.validate  # noqa: F401
        except ImportError:
            self.skipTest("vaws_knowledge.validate is not importable")
        result = run_runner(
            "--schema-cmd",
            impl("gate_tools_adapter.py", "schema"),
            "--only",
            "schema-",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("PASS", status_of(result.stdout, ACCEPT_VECTOR))
        self.assertEqual("PASS", status_of(result.stdout, REJECT_VECTOR))
        self.assertEqual(
            "PASS", status_of(result.stdout, "schema-prose-as-evidence")
        )

    def test_redaction_adapter_accepts_clean_and_rejects_ipv4(self):
        try:
            import vaws_knowledge.redact  # noqa: F401
        except ImportError:
            self.skipTest("vaws_knowledge.redact is not importable")
        result = run_runner(
            "--redaction-cmd",
            impl("gate_tools_adapter.py", "redaction"),
            "--only",
            "redaction-",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "PASS", status_of(result.stdout, "redaction-clean-control")
        )
        self.assertEqual("PASS", status_of(result.stdout, "redaction-ipv4"))

    def test_conflicts_adapter_runs_the_real_bot_gate(self):
        # bot/conflicts.py, not a regex: the contradicting pair must be found
        # by the same code that runs on a pull request.
        try:
            import vaws_knowledge.bot.conflicts  # noqa: F401
        except ImportError:
            self.skipTest("vaws_knowledge.bot.conflicts is not importable")
        result = run_runner(
            "--conflicts-cmd",
            impl("gate_tools_adapter.py", "conflicts"),
            "--only",
            "conflicts-",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "PASS", status_of(result.stdout, "conflicts-measurement-contradicting-value")
        )
        self.assertEqual(
            "PASS", status_of(result.stdout, "conflicts-theoretical-and-sustained-control")
        )
        self.assertEqual(
            "PASS",
            status_of(result.stdout, "conflicts-measurement-disjoint-coordinate-control"),
        )

    def test_export_adapter_runs_the_real_exporter(self):
        # export_stable.py proves the runner compares two runs. It does not
        # prove tools/export.py is idempotent, which is the claim that matters
        # for the corpus - and the measurement vector is there because a
        # numeric string is the part most likely to move under a round trip.
        try:
            import vaws_knowledge.export  # noqa: F401
        except ImportError:
            self.skipTest("vaws_knowledge.export is not importable")
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed; the exporter needs it")
        result = run_runner(
            "--export-cmd",
            impl("gate_tools_adapter.py", "export"),
            "--only",
            "export-",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "PASS", status_of(result.stdout, "export-idempotent-measurement-entry")
        )
        self.assertEqual(
            "PASS", status_of(result.stdout, "export-idempotent-unchanged-entry")
        )

    def test_validator_crash_does_not_pass_a_negative_vector(self):
        # The adapter must invoke the real tools/validate.py API. Shadowing
        # jsonschema with a module that raises on import crashes that API
        # before ValidationResult exists. That is not a semantic reject.
        try:
            import vaws_knowledge.validate  # noqa: F401
        except ImportError:
            self.skipTest("vaws_knowledge.validate is not importable")
        fault = FIXTURES / "fault_jsonschema"
        cmd = json.dumps([
            PY, "-c", "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
            "script=sys.argv.pop(1);sys.argv[0]=script;runpy.run_path(script,run_name='__main__')",
            str(fault), str(FIXTURES / "gate_tools_adapter.py"), "schema",
        ])
        result = run_runner("--schema-cmd", cmd, "--only", REJECT_VECTOR)
        combined = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, combined)
        self.assertEqual("FAIL", status_of(result.stdout, REJECT_VECTOR), combined)
        self.assertNotEqual("PASS", status_of(result.stdout, REJECT_VECTOR), combined)
        self.assertIn("execution/protocol failure", result.stdout)
        self.assertNotIn("1416a279-1215-4adf-a978-82b40a3be0bc", combined)


if __name__ == "__main__":
    unittest.main()
