"""The vector inventory covers the behaviours the kit claims to cover.

conformance/selfcheck.py proves the vectors are internally consistent. It
cannot prove they are sufficient - a kit with one vector is perfectly
consistent and worthless. So the coverage claims made in conformance/README.md
are asserted here, against the vector files themselves, so that deleting a
vector breaks a test instead of quietly narrowing the standard.

Each assertion below corresponds to a canonicalization behaviour in
docs/federation.md, or to a gate behaviour a conforming client must get right.
"""

import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
VECTORS = REPO / "conformance" / "vectors"
GATE_VECTORS = REPO / "conformance" / "gate_vectors"

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required by the conformance kit: {exc}") from exc


def load(directory):
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.yaml"))
    ]


class CanonicalizationCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = load(VECTORS)
        cls.by_id = {vector["id"]: vector for vector in cls.vectors}

    def test_the_anchor_is_present(self):
        self.assertIn("anchor-valid-entry", self.by_id)

    def test_only_scope_and_rule_participate(self):
        # Proved by a vector that changes everything else and keeps the hash.
        anchor = self.by_id["anchor-valid-entry"]
        mutated = self.by_id["anchor-metadata-mutated"]
        self.assertEqual(
            anchor["expected_content_hash"], mutated["expected_content_hash"]
        )
        entry = mutated["entry"]
        self.assertNotEqual(
            anchor["entry"]["status"], entry["status"], "the vector changed no status"
        )
        self.assertNotIn("verification", entry)
        self.assertNotEqual(anchor["entry"]["provenance"], entry["provenance"])
        self.assertNotEqual(anchor["entry"]["lifecycle"], entry["lifecycle"])

    def test_key_ordering_independence(self):
        scrambled = self.by_id["anchor-key-order-scrambled"]
        self.assertEqual(
            self.by_id["anchor-valid-entry"]["expected_content_hash"],
            scrambled["expected_content_hash"],
        )
        self.assertEqual(
            ["rule", "scope"][::-1],
            [key for key in ("scope", "rule") if key in scrambled["entry"]],
            "the scrambled vector no longer scrambles the payload keys",
        )

    def test_fingerprint_normalization_is_covered_in_full(self):
        fingerprints = self.by_id["fingerprints-normalization"]["entry"]["rule"][
            "fingerprints"
        ]
        self.assertTrue(any(item != item.lower() for item in fingerprints), "no case")
        self.assertTrue(
            any(item != item.strip() for item in fingerprints if item.strip()),
            "no surrounding whitespace",
        )
        self.assertTrue(any("  " in item or "\t" in item for item in fingerprints), "no runs")
        self.assertTrue(any(not item.strip() for item in fingerprints), "no empties")
        expected = self.by_id["fingerprints-normalization"]["expected_payload"]
        self.assertLess(
            expected.count('"gloo makedeviceforhostname"'),
            2,
            "duplicates were not de-duplicated",
        )
        self.assertIn("fingerprints-all-empty", self.by_id)

    def test_line_ending_normalization_is_covered(self):
        crlf = self.by_id["line-endings-crlf"]["entry"]["rule"]
        self.assertIn("\r\n", crlf["symptom"] + crlf["root_cause"])
        lone = self.by_id["line-endings-lone-cr"]["entry"]["rule"]
        joined = lone["symptom"] + lone["root_cause"]
        self.assertIn("\r", joined)
        self.assertTrue(
            any(
                char == "\r" and joined[index + 1 : index + 2] != "\n"
                for index, char in enumerate(joined)
            ),
            "the lone-CR vector contains no lone CR",
        )
        for vector_id in ("line-endings-crlf", "line-endings-lone-cr"):
            self.assertNotIn("\\r", self.by_id[vector_id]["expected_payload"])

    def test_outer_whitespace_and_no_reflow_is_covered(self):
        vector = self.by_id["outer-whitespace-and-no-reflow"]
        entry = vector["entry"]
        self.assertTrue(entry["rule"]["summary"] != entry["rule"]["summary"].strip())
        self.assertTrue(
            any(
                value != value.strip()
                for value in entry["scope"]["topology"]["values"]
            ),
            "scope values carry no padding",
        )
        self.assertIn("  ", vector["expected_payload"], "internal runs were collapsed")

    def test_non_ascii_content_is_covered(self):
        payload = self.by_id["non-ascii-content"]["expected_payload"]
        self.assertNotIn("\\u", payload, "the expected payload is ASCII-escaped")
        self.assertTrue(any(ord(char) > 127 for char in payload))
        self.assertNotIn('", "', payload, "the expected payload uses padded separators")
        self.assertNotIn('": "', payload, "the expected payload uses padded separators")

    def test_every_constraint_form_is_covered(self):
        scope = self.by_id["scope-constraint-forms"]["entry"]["scope"]
        forms = set()
        for constraint in scope.values():
            forms.update(constraint.keys())
        self.assertEqual({"values", "range", "any", "basis"}, forms)
        ranges = [c["range"] for c in scope.values() if "range" in c]
        self.assertTrue(any(r["min"] is None or r["max"] is None for r in ranges))
        self.assertTrue(any(r["min"] and r["max"] for r in ranges))

    def test_absent_optional_keys_are_covered(self):
        rule = self.by_id["rule-optional-keys-absent"]["entry"]["rule"]
        self.assertNotIn("avoidance", rule)
        self.assertNotIn("fingerprints", rule)


class GateCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = load(GATE_VECTORS)
        cls.by_id = {vector["id"]: vector for vector in cls.vectors}

    def _of_gate(self, gate):
        return [v for v in self.vectors if v["gate"] == gate]

    def test_redaction_refuses_all_five_required_shapes(self):
        classes = {
            vector["offending"]["class"]
            for vector in self._of_gate("redaction")
            if vector["expected_verdict"] == "reject"
        }
        self.assertEqual(
            {"ipv4", "hostname", "user_path", "email", "credential"}, classes
        )

    def test_schema_refuses_the_four_required_cases(self):
        rejections = [
            vector["id"]
            for vector in self._of_gate("schema")
            if vector["expected_verdict"] == "reject"
        ]
        self.assertEqual(
            sorted(
                [
                    "schema-any-without-basis",
                    "schema-omitted-scope-dimension",
                    "schema-prose-as-evidence",
                    "schema-verified-without-confirmation",
                ]
            ),
            sorted(rejections),
        )

    def test_export_idempotence_is_covered(self):
        exports = self._of_gate("export")
        self.assertTrue(exports)
        for vector in exports:
            self.assertEqual("byte-identical", vector["expected_verdict"])
            self.assertGreaterEqual(vector["runs"], 2)

    def test_each_refusing_gate_has_an_accept_control(self):
        # Without a control, a gate that refuses everything passes the kit.
        for gate in ("redaction", "schema"):
            verdicts = {v["expected_verdict"] for v in self._of_gate(gate)}
            self.assertIn("accept", verdicts, f"{gate} has no accept control")
            self.assertIn("reject", verdicts, f"{gate} has no rejection vector")


if __name__ == "__main__":
    unittest.main()
