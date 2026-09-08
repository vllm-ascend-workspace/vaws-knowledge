"""Contract regression suite for schemas/knowledge-v2.schema.json.

Every rule in the schema is proved here by a rejection, not by a passing
example. A schema that quietly accepts everything is worse than no schema: it
produces a corpus that looks validated. So each case below removes or corrupts
exactly one property and asserts the document stops validating.

If a case here starts failing, the contract was loosened. That is a decision to
make deliberately, in a PR that says so — not a test to update.

Requires jsonschema (a package dependency).
"""

import copy
import json
import pathlib
import unittest

from vaws_knowledge._common import SCHEMA_PATH as PACKAGED_SCHEMA

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = pathlib.Path(PACKAGED_SCHEMA)
FIXTURE_PATH = REPO / "examples" / "valid-entry.yaml"

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required to read the fixture: {exc}") from exc

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(
        "jsonschema is required for the contract suite. "
        "Install it with: python3 -m pip install -e ."
    ) from exc


def _mutation(description):
    """Mark a method as a negative case and carry its human description."""

    def decorate(fn):
        fn.description = description
        return fn

    return decorate


class KnowledgeSchemaContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = Draft202012Validator(cls.schema)
        cls.doc = yaml.safe_load(FIXTURE_PATH.read_text())

    def _mutate(self, fn):
        doc = copy.deepcopy(self.doc)
        fn(doc["entries"][0], doc)
        return doc

    def _assert_rejected(self, description, fn):
        doc = self._mutate(fn)
        errors = list(self.validator.iter_errors(doc))
        self.assertTrue(errors, f"schema accepted {description}; the contract is loose")

    # -- the schema itself -------------------------------------------------

    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(self.schema)

    def test_reference_fixture_validates(self):
        errors = sorted(self.validator.iter_errors(self.doc), key=lambda e: list(e.path))
        details = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        self.assertEqual([], errors, f"reference fixture must validate: {details}")

    # -- the applicability coordinate has no "omitted" state ---------------

    def test_rejects_omitted_scope_dimension(self):
        self._assert_rejected(
            "an omitted scope dimension",
            lambda e, d: e["scope"].pop("driver"),
        )

    def test_rejects_independence_claim_without_basis(self):
        self._assert_rejected(
            "an 'any' claim with no stated basis",
            lambda e, d: e["scope"].update(soc={"any": True}),
        )

    def test_rejects_stub_basis(self):
        self._assert_rejected(
            "an 'any' basis too short to be a real claim",
            lambda e, d: e["scope"].update(soc={"any": True, "basis": "dunno"}),
        )

    def test_rejects_constraint_mixing_bounded_and_any(self):
        self._assert_rejected(
            "a constraint that is both bounded and independent",
            lambda e, d: e["scope"].update(
                topology={"values": ["tp8"], "any": True, "basis": "x" * 20}
            ),
        )

    def test_rejects_half_open_range_written_as_missing_bound(self):
        # Unbounded is expressed as an explicit null, so that "untested above
        # this version" is a visible claim rather than an absent key.
        self._assert_rejected(
            "a range missing one of its bounds",
            lambda e, d: e["scope"].update(torch={"range": {"min": "2.5.0"}}),
        )

    def test_rejects_yaml_float_where_version_string_required(self):
        # `min: 2.5` in YAML is a float, and would otherwise silently compare
        # wrong against a real version string.
        self._assert_rejected(
            "a float version bound",
            lambda e, d: e["scope"].update(torch={"range": {"min": 2.5, "max": None}}),
        )

    # -- the schema is the egress whitelist --------------------------------

    def test_rejects_undeclared_entry_field(self):
        self._assert_rejected(
            "an undeclared entry field",
            lambda e, d: e.update(internal_note="leaked field"),
        )

    def test_rejects_undeclared_scope_dimension(self):
        self._assert_rejected(
            "an undeclared scope dimension",
            lambda e, d: e["scope"].update(secret_dimension={"any": True, "basis": "x" * 20}),
        )

    # -- verified means somebody else confirmed it, with a reference -------

    def test_rejects_verified_without_verification_record(self):
        self._assert_rejected(
            "status verified with no verification record",
            lambda e, d: e.pop("verification"),
        )

    def test_rejects_verified_without_evidence(self):
        self._assert_rejected(
            "status verified with empty evidence",
            lambda e, d: e["verification"].update(evidence=[]),
        )

    def test_rejects_verified_without_confirmation(self):
        self._assert_rejected(
            "status verified that nobody confirmed",
            lambda e, d: e["verification"].update(verified_by=[]),
        )

    def test_rejects_prose_as_evidence(self):
        self._assert_rejected(
            "prose in place of an evidence reference",
            lambda e, d: e["verification"].update(evidence=["it worked when I tried it"]),
        )

    def test_rejects_invented_evidence_type(self):
        self._assert_rejected(
            "an invented evidence type",
            lambda e, d: e["verification"]["evidence"][0].update(type="i-tested-it"),
        )

    def test_rejects_incomplete_concrete_environment(self):
        self._assert_rejected(
            "verified_against missing a required dimension",
            lambda e, d: e["verification"]["verified_against"].pop("cann"),
        )

    def test_stale_still_requires_a_verification_record(self):
        # stale means "was established, not re-checked recently" — it is not a
        # way to keep an unevidenced claim in the verified corpus.
        def mutate(e, d):
            e["status"] = "stale"
            e.pop("verification")

        self._assert_rejected("status stale with no verification record", mutate)

    # -- lifecycle honesty --------------------------------------------------

    def test_rejects_resolved_without_fix_reference(self):
        self._assert_rejected(
            "status resolved with no resolved_by reference",
            lambda e, d: e.update(status="resolved"),
        )

    def test_rejects_high_confidence_on_unverified_claim(self):
        def mutate(e, d):
            e["status"] = "unverified"
            e["confidence"] = "high"
            e.pop("verification")

        self._assert_rejected("confidence high on an unverified claim", mutate)

    # -- identity and sync keys --------------------------------------------

    def test_rejects_malformed_uuid(self):
        self._assert_rejected(
            "a malformed uuid",
            lambda e, d: e.update(uuid="not-a-uuid"),
        )

    def test_rejects_content_hash_without_algorithm_prefix(self):
        self._assert_rejected(
            "a content_hash with no sha256: prefix",
            lambda e, d: e.update(content_hash="32d1e6611f47083c885205b4f4ef398ea238c0e7"),
        )

    # -- re-scan bookkeeping is separate from export-time provenance -------

    def test_accepts_redaction_cleared_under(self):
        doc = self._mutate(lambda e, d: e.update(redaction_cleared_under="r2"))
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual([], errors, "a re-scanned entry must be able to record its profile")

    def test_rejects_malformed_redaction_cleared_under(self):
        self._assert_rejected(
            "a cleared-under value that is not a profile identifier",
            lambda e, d: e.update(redaction_cleared_under="cleaned"),
        )

    def test_cleared_under_does_not_change_the_revision(self):
        # The revision tracks what an entry claims. Re-scanning it is
        # processing, so it must not look like a new revision to sync.
        import hashlib
        import json

        def payload(entry):
            rule = json.loads(json.dumps(entry["rule"]))
            fps = rule.get("fingerprints")
            if fps is not None:
                rule["fingerprints"] = sorted({" ".join(f.lower().split()) for f in fps if f.strip()})
            text = json.dumps(
                {"rule": rule, "scope": entry["scope"]},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

        before = payload(self.doc["entries"][0])
        after = payload(self._mutate(lambda e, d: e.update(redaction_cleared_under="r9"))["entries"][0])
        self.assertEqual(before, after)
        self.assertEqual(self.doc["entries"][0]["content_hash"], before)

    # -- document level -----------------------------------------------------

    def test_rejects_wrong_schema_version(self):
        self._assert_rejected("a v1 document offered as v2", lambda e, d: d.update(schema_version=1))

    def test_rejects_layer_outside_the_two_review_zones(self):
        # The candidate layer is local and untracked; it is not a corpus layer.
        self._assert_rejected("layer=candidate in the corpus", lambda e, d: d.update(layer="candidate"))

    # -- conflicts name real dimensions ------------------------------------

    def test_rejects_conflict_naming_unknown_dimension(self):
        self._assert_rejected(
            "a conflict naming a dimension that does not exist",
            lambda e, d: e.update(
                conflicts=[
                    {
                        "with": "1416a279-1215-4adf-a978-82b40a3be0bd",
                        "undeclared_dimensions": ["phase_of_moon"],
                        "recorded_at": "2026-09-07",
                    }
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
