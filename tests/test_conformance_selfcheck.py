"""The conformance kit checks its own vectors, and that check is itself proved.

`conformance/selfcheck.py` is the only thing standing between a typo in a
vector and four implementations being "corrected" until they all reproduce the
same wrong hash. So this suite does two things:

1. runs the real self-check over the real vectors, and requires it to be clean;
2. tampers with a throwaway copy of the vectors, one defect at a time, and
   requires the self-check to catch each one.

The second half is the same idea as tests/test_schema_contract.py: a checker
that accepts everything is worse than no checker, because its output looks like
assurance. If a tamper case here starts passing, the self-check went blind.
"""

import contextlib
import hashlib
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
KIT = REPO / "conformance"

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required by the conformance kit: {exc}") from exc

sys.path.insert(0, str(KIT))

import reference  # noqa: E402
import selfcheck  # noqa: E402


def run_selfcheck():
    """Run the self-check with its output captured."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        problems = selfcheck.run(quiet=True)
    return problems, buffer.getvalue()


class SelfCheckOverTheRealVectors(unittest.TestCase):
    def test_kit_is_self_consistent(self):
        problems, _ = run_selfcheck()
        self.assertEqual(
            [], problems.items, "the shipped vectors are not internally consistent"
        )

    def test_self_check_actually_checks_something(self):
        problems, _ = run_selfcheck()
        self.assertGreater(
            problems.checks, 100, "the self-check ran suspiciously few checks"
        )

    def test_reference_reproduces_the_anchor_recorded_in_examples(self):
        example = REPO / "examples" / "valid-entry.yaml"
        if not example.is_file():
            self.skipTest("examples/valid-entry.yaml is not in this checkout")
        entry = yaml.safe_load(example.read_text(encoding="utf-8"))["entries"][0]
        self.assertEqual(entry["content_hash"], reference.content_hash(entry))


class SelfCheckCatchesTamperedVectors(unittest.TestCase):
    """Each case breaks one thing and requires the self-check to notice."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="conformance-tamper-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(KIT / "vectors", self.root / "vectors")
        shutil.copytree(KIT / "gate_vectors", self.root / "gate_vectors")

    def _run(self, example=None):
        patches = {
            "VECTORS": self.root / "vectors",
            "GATE_VECTORS": self.root / "gate_vectors",
        }
        if example is not None:
            patches["EXAMPLE"] = example
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(selfcheck, name, value))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                problems = selfcheck.run(quiet=True)
        return problems

    def _assert_caught(self, fragment, problems):
        joined = "\n".join(problems.items)
        self.assertTrue(problems.items, "the self-check accepted a tampered vector")
        self.assertIn(
            fragment,
            joined,
            f"the self-check complained, but not about {fragment!r}:\n{joined}",
        )

    def _vector(self, name):
        return self.root / "vectors" / name

    def _gate_vector(self, name):
        return self.root / "gate_vectors" / name

    def _rewrite(self, path, mutate):
        """Rewrite a vector with a consistent payload and hash after mutation.

        Used for tamper cases that must not trip the hash/payload check, so
        that the case proves the specific check it is aiming at.
        """
        vector = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(vector)
        text = path.read_text(encoding="utf-8")
        head = text[: text.index("expected_payload:")]
        entry_block = yaml.safe_dump(
            {"entry": vector["entry"]}, sort_keys=False, allow_unicode=True, width=100
        )
        head = head[: head.index("\nentry:") + 1] + entry_block
        payload = reference.canonical_json(vector["entry"])
        digest = reference.content_hash(vector["entry"])
        path.write_text(
            f"{head}expected_payload: |-\n  {payload}\n"
            f"expected_content_hash: {digest}\n",
            encoding="utf-8",
        )

    # -- the core invariant: hash == sha256(payload) -----------------------

    def test_catches_hash_that_is_not_the_hash_of_its_payload(self):
        path = self._vector("anchor-valid-entry.yaml")
        text = path.read_text(encoding="utf-8")
        head, _, tail = text.rpartition("expected_content_hash: sha256:")
        path.write_text(f"{head}expected_content_hash: sha256:{'a' + tail[1:]}")
        self._assert_caught("not the sha256 of expected_payload", self._run())

    def test_catches_payload_not_in_step_4_serialization_form(self):
        path = self._vector("rule-optional-keys-absent.yaml")
        vector = yaml.safe_load(path.read_text(encoding="utf-8"))
        loose = json.dumps(  # padded separators, escaped non-ASCII
            json.loads(vector["expected_payload"]), sort_keys=True
        )
        text = path.read_text(encoding="utf-8")
        head = text[: text.index("expected_payload:")]
        digest = hashlib.sha256(loose.encode("utf-8")).hexdigest()
        path.write_text(
            f"{head}expected_payload: |-\n  {loose}\n"
            f"expected_content_hash: sha256:{digest}\n"
        )
        self._assert_caught("not in step 4 form", self._run())

    def test_catches_entry_that_does_not_canonicalize_to_its_payload(self):
        path = self._vector("scope-constraint-forms.yaml")
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "summary: Fixture entry exercising every constraint form",
                "summary: Something else entirely",
            )
        )
        self._assert_caught("does not canonicalize to expected_payload", self._run())

    def test_catches_invariance_group_members_that_disagree(self):
        def mutate(vector):
            vector["entry"]["rule"]["summary"] = "A different claim"

        self._rewrite(self._vector("anchor-metadata-mutated.yaml"), mutate)
        self._assert_caught("members disagree", self._run())

    def test_catches_anchor_drift_from_the_example(self):
        example = self.root / "drifted-example.yaml"
        original = yaml.safe_load(
            (REPO / "examples" / "valid-entry.yaml").read_text(encoding="utf-8")
        )
        original["entries"][0]["rule"]["summary"] = "Reworded after the vector froze"
        example.write_text(yaml.safe_dump(original, allow_unicode=True))
        self._assert_caught("no longer match examples/valid-entry.yaml", self._run(example))

    def test_catches_an_nfc_composed_expected_payload_for_nfd_input(self):
        import unicodedata

        path = self._vector("no-unicode-normalization.yaml")
        vector = yaml.safe_load(path.read_text(encoding="utf-8"))
        nfc_payload = unicodedata.normalize("NFC", vector["expected_payload"])
        self.assertNotEqual(
            nfc_payload,
            vector["expected_payload"],
            "the no-unicode-normalization vector has no NFD sequence to pin",
        )
        digest = hashlib.sha256(nfc_payload.encode("utf-8")).hexdigest()
        text = path.read_text(encoding="utf-8")
        head = text[: text.index("expected_payload:")]
        path.write_text(
            f"{head}expected_payload: |-\n  {nfc_payload}\n"
            f"expected_content_hash: sha256:{digest}\n",
            encoding="utf-8",
        )
        self._assert_caught("does not canonicalize to expected_payload", self._run())

    # -- gate vectors ------------------------------------------------------

    def _declared_ipv4(self):
        """The address the ipv4 vector declares, read rather than written.

        Address literals live in conformance/gate_vectors/ and nowhere else in
        the repository, so these cases derive what they need from the vector.
        """
        path = self._gate_vector("redaction-ipv4.yaml")
        vector = yaml.safe_load(path.read_text(encoding="utf-8"))
        return path, str(vector["offending"]["value"])

    def test_catches_declared_offending_value_that_is_not_in_the_document(self):
        path, declared = self._declared_ipv4()
        # Still inside the same documentation range, but not the address the
        # document actually contains, so the vector would test nothing.
        absent = declared.rsplit(".", 1)[0] + ".254"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                f"  value: {declared}", f"  value: {absent}"
            ),
            encoding="utf-8",
        )
        self._assert_caught("does not appear in the", self._run())

    def test_catches_a_fixture_value_that_is_not_a_reserved_range(self):
        path, declared = self._declared_ipv4()
        # Assembled from octets rather than written out: it is outside every
        # reserved range the kit allows, which is the whole point of the case,
        # and no address literal belongs in a tracked file outside
        # conformance/gate_vectors/. A private-range address is what a real
        # leak usually looks like.
        not_reserved = ".".join(("172", "31", "4", "9"))
        path.write_text(
            path.read_text(encoding="utf-8").replace(declared, not_reserved),
            encoding="utf-8",
        )
        self._assert_caught("not drawn from a reserved documentation range", self._run())

    def test_catches_a_gate_vector_without_the_synthetic_banner(self):
        path = self._gate_vector("redaction-email.yaml")
        lines = path.read_text(encoding="utf-8").splitlines(True)
        path.write_text("".join(lines[1:]), encoding="utf-8")
        self._assert_caught("synthetic-values banner", self._run())

    def test_catches_a_stale_content_hash_inside_a_gate_document(self):
        path = self._gate_vector("schema-valid-control.yaml")
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "summary: Distributed init fails",
                "summary: Reworded without rehashing, distributed init fails",
            ),
            encoding="utf-8",
        )
        self._assert_caught("content_hash is stale", self._run())

    def test_catches_an_accept_control_that_declares_a_defect(self):
        _, declared = self._declared_ipv4()
        path = self._gate_vector("redaction-clean-control.yaml")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "notes: >-",
                "offending:\n  class: ipv4\n"
                f"  value: {declared}\n"
                "  location: nowhere\n  synthetic_source: RFC 5737\nnotes: >-",
            ),
            encoding="utf-8",
        )
        self._assert_caught("must not declare an offending value", self._run())

    # -- kit hygiene -------------------------------------------------------

    def test_catches_a_kit_file_that_imports_the_implementation(self):
        fake_kit = self.root / "fake_kit"
        fake_kit.mkdir()
        (fake_kit / "runner.py").write_text(
            "from tools.canonical import content_hash\n", encoding="utf-8"
        )
        problems = selfcheck.Problems(quiet=True)
        with mock.patch.object(selfcheck, "HERE", fake_kit):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                selfcheck.check_kit_independence(problems)
        self._assert_caught("imports", problems)


class GateVectorsAgainstTheRealSchema(unittest.TestCase):
    """The schema gate vectors must fail for the reason they claim."""

    def setUp(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest(
                "jsonschema is not installed; install it with "
                "python3 -m pip install jsonschema to check gate vectors "
                "against schemas/knowledge-v2.schema.json"
            )
        if not (REPO / "schemas" / "knowledge-v2.schema.json").is_file():
            self.skipTest("schemas/knowledge-v2.schema.json is not in this checkout")

    def test_each_gate_vector_has_exactly_the_defect_it_declares(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            problems = selfcheck.Problems(quiet=True)
            vectors = selfcheck.check_gate_vectors(problems)
            selfcheck.check_gate_vectors_against_schema(problems, vectors)
        self.assertEqual([], problems.items)
        self.assertEqual([], problems.notes, "the schema check did not actually run")


if __name__ == "__main__":
    unittest.main()
