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

import vaws_knowledge.conformance as _conformance_pkg

KIT = pathlib.Path(_conformance_pkg.__file__).resolve().parent
VECTORS = KIT / "vectors"
GATE_VECTORS = KIT / "gate_vectors"

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

    def test_the_measurement_body_variant_is_covered(self):
        vector = self.by_id["measurement-body-payload-key"]
        self.assertIn("measurement", vector["entry"])
        self.assertNotIn("rule", vector["entry"])
        # The payload is keyed by the body's own name. That is the whole
        # mechanism by which adding the variant moved no rule entry's hash.
        self.assertTrue(vector["expected_payload"].startswith('{"measurement":{'))
        self.assertNotIn('"rule"', vector["expected_payload"])

    def test_the_measurement_body_has_an_invariance_group(self):
        members = [
            v["id"] for v in self.vectors if v.get("invariance_group") == "measurement-body"
        ]
        self.assertGreater(len(members), 1, "an invariance group with one member proves nothing")
        hashes = {
            self.by_id[m]["expected_content_hash"].strip() for m in members
        }
        self.assertEqual(1, len(hashes), members)

    def test_no_two_vectors_claim_two_bodies_or_none(self):
        for vector in self.vectors:
            present = [k for k in ("rule", "measurement") if k in vector["entry"]]
            self.assertEqual(1, len(present), f"{vector['id']} declares bodies {present}")

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
        self.assertNotEqual(
            anchor["entry"]["verification"]["last_verified_at"],
            entry["verification"]["last_verified_at"],
            "the vector did not change a revalidation date",
        )
        self.assertNotEqual(
            anchor["entry"]["verification"]["verified_by"],
            entry["verification"]["verified_by"],
            "the vector did not change a reviewer identity",
        )
        self.assertNotEqual(
            anchor["entry"]["provenance"]["redaction_profile"],
            entry["provenance"]["redaction_profile"],
            "the vector did not change redaction_profile",
        )
        self.assertEqual("r99", entry["redaction_cleared_under"])
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

    def test_nbsp_and_ideographic_space_are_preserved(self):
        vector = self.by_id["non-ascii-whitespace-preserved"]
        summary = vector["entry"]["rule"]["summary"]
        self.assertIn("\u00a0", summary)
        self.assertIn("\u3000", summary)
        fingerprints = vector["entry"]["rule"]["fingerprints"]
        self.assertTrue(any("\u00a0" in item for item in fingerprints))
        self.assertTrue(any("\u3000" in item for item in fingerprints))
        payload = vector["expected_payload"]
        self.assertIn("\u00a0", payload)
        self.assertIn("\u3000", payload)
        self.assertIn('"c d"', payload)

    def test_ascii_lowercase_preserves_non_ascii_letters(self):
        fingerprints = self.by_id["ascii-lowercase-preserves-nonascii"]["entry"]["rule"][
            "fingerprints"
        ]
        self.assertTrue(any("İ" in item for item in fingerprints))
        self.assertTrue(any("É" in item for item in fingerprints))
        self.assertTrue(any("Σ" in item for item in fingerprints))
        payload = self.by_id["ascii-lowercase-preserves-nonascii"]["expected_payload"]
        self.assertIn("abc İ É Σ", payload)
        self.assertNotIn("i\u0307", payload)

    def test_interior_line_trailing_whitespace_is_stripped_in_rule_prose(self):
        summary = self.by_id["line-trailing-ascii-whitespace"]["entry"]["rule"]["summary"]
        self.assertIn("  \n", summary)
        payload = self.by_id["line-trailing-ascii-whitespace"]["expected_payload"]
        self.assertIn("First line\\nSecond line", payload)
        self.assertNotIn("First line  \\n", payload)

    def test_interior_line_trailing_whitespace_is_stripped_in_nested_scope(self):
        basis = self.by_id["nested-scope-line-trailing-whitespace"]["entry"]["scope"]["soc"][
            "basis"
        ]
        self.assertIn("\t \n", basis)
        values = self.by_id["nested-scope-line-trailing-whitespace"]["entry"]["scope"][
            "topology"
        ]["values"]
        self.assertTrue(any("  \n  " in item for item in values))
        payload = self.by_id["nested-scope-line-trailing-whitespace"]["expected_payload"]
        self.assertIn("Synthetic first line\\nsecond line", payload)
        self.assertIn("line one\\n  indented line two", payload)

    def test_fingerprint_byte_order_is_utf8(self):
        authored = self.by_id["fingerprint-byte-order"]["entry"]["rule"]["fingerprints"]
        self.assertEqual(["я", "é", "z", "a"], authored)
        payload = self.by_id["fingerprint-byte-order"]["expected_payload"]
        self.assertIn('["a","z","é","я"]', payload)

    def test_no_unicode_normalization_is_applied(self):
        import unicodedata

        vector = self.by_id["no-unicode-normalization"]
        summary = vector["entry"]["rule"]["summary"]
        self.assertNotEqual(unicodedata.normalize("NFC", summary), summary)
        self.assertEqual(unicodedata.normalize("NFD", summary), summary)
        payload = vector["expected_payload"]
        self.assertIn("\u0301", payload)
        self.assertNotEqual(unicodedata.normalize("NFC", payload), payload)


class GateCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = load(GATE_VECTORS)
        cls.by_id = {vector["id"]: vector for vector in cls.vectors}

    def _of_gate(self, gate):
        return [v for v in self.vectors if v["gate"] == gate]

    def test_redaction_refuses_all_six_required_shapes(self):
        classes = {
            vector["offending"]["class"]
            for vector in self._of_gate("redaction")
            if vector["expected_verdict"] == "reject"
        }
        self.assertEqual(
            {"ipv4", "hostname", "user_path", "email", "credential", "machine_identifier"},
            classes,
        )

    def test_the_machine_identifier_vector_is_not_a_second_hostname_vector(self):
        # The two are separate because the token shapes are separate: a gate
        # can catch the hostname by looking for a plausible domain suffix, and
        # that finds nothing in "remote 000".
        machine = self.by_id["redaction-internal-machine-identifier"]["offending"]["value"]
        hostname = self.by_id["redaction-internal-hostname"]["offending"]["value"]
        self.assertNotIn(".", machine)
        self.assertIn(".", hostname)
        self.assertNotEqual(machine, hostname)

    def test_the_machine_identifier_vector_keeps_the_method_evidence(self):
        # Scrubbing the machine must not scrub what makes the number checkable.
        text = yaml.safe_dump(
            self.by_id["redaction-internal-machine-identifier"]["document"],
            allow_unicode=True,
        )
        for load_bearing in ("NPU 4", "torch_npu", "dense matmul", "torch.npu.Event"):
            self.assertIn(load_bearing, text)

    def test_schema_refuses_the_six_required_cases(self):
        rejections = [
            vector["id"]
            for vector in self._of_gate("schema")
            if vector["expected_verdict"] == "reject"
        ]
        self.assertEqual(
            sorted(
                [
                    "schema-any-without-basis",
                    "schema-measurement-quantity-without-unit",
                    "schema-measurement-verified-without-confirmation",
                    "schema-omitted-scope-dimension",
                    "schema-prose-as-evidence",
                    "schema-verified-without-confirmation",
                ]
            ),
            sorted(rejections),
        )

    def test_the_measurement_body_has_its_own_valid_control(self):
        control = self.by_id["schema-measurement-valid-control"]
        self.assertEqual("accept", control["expected_verdict"])
        entry = control["document"]["entries"][0]
        self.assertIn("measurement", entry)
        self.assertNotIn("rule", entry)

    def test_conflicts_covers_contradiction_and_both_non_conflicts(self):
        conflicts = {v["id"]: v for v in self._of_gate("conflicts")}
        self.assertEqual("reject", conflicts["conflicts-measurement-contradicting-value"]["expected_verdict"])
        # Two accept controls, and each rules out a different wrong gate.
        # Keying conflicts on the quantity name alone fails the first; ignoring
        # the coordinate fails the second.
        self.assertEqual("accept", conflicts["conflicts-theoretical-and-sustained-control"]["expected_verdict"])
        self.assertEqual(
            "accept",
            conflicts["conflicts-measurement-disjoint-coordinate-control"]["expected_verdict"],
        )

    def test_the_contradiction_vector_differs_only_in_the_value(self):
        # If anything else differed, a prose comparison could reach the right
        # verdict for the wrong reason and the vector would prove nothing.
        vector = self.by_id["conflicts-measurement-contradicting-value"]
        a, b = vector["document"]["entries"]
        self.assertEqual(a["scope"], b["scope"])
        self.assertEqual(a["measurement"]["subject"], b["measurement"]["subject"])
        qa = a["measurement"]["quantities"][0]
        qb = b["measurement"]["quantities"][0]
        self.assertEqual(
            {k: v for k, v in qa.items() if k != "value"},
            {k: v for k, v in qb.items() if k != "value"},
        )
        self.assertNotEqual(qa["value"], qb["value"])
        self.assertEqual(sorted([qa["value"], qb["value"]]), sorted(vector["contradiction"]["values"]))

    def test_the_sustained_control_shares_the_quantity_name_but_not_the_basis(self):
        entries = self.by_id["conflicts-theoretical-and-sustained-control"]["document"]["entries"]
        qa = entries[0]["measurement"]["quantities"][0]
        qb = entries[1]["measurement"]["quantities"][0]
        self.assertEqual(qa["name"], qb["name"])
        self.assertNotEqual(qa["basis"], qb["basis"])
        self.assertNotEqual(qa["value"], qb["value"])

    def test_export_idempotence_is_covered(self):
        exports = self._of_gate("export")
        self.assertTrue(exports)
        for vector in exports:
            self.assertEqual("byte-identical", vector["expected_verdict"])
            self.assertGreaterEqual(vector["runs"], 2)

    def test_export_idempotence_covers_both_bodies(self):
        # An exporter can be perfectly stable on rule prose and unstable on a
        # numeric string, so one body's vectors do not cover the other's.
        bodies = set()
        for vector in self._of_gate("export"):
            for entry in vector["document"]["entries"]:
                bodies |= {k for k in ("rule", "measurement") if k in entry}
        self.assertEqual({"rule", "measurement"}, bodies)

    def test_each_refusing_gate_has_an_accept_control(self):
        # Without a control, a gate that refuses everything passes the kit.
        for gate in ("redaction", "schema", "conflicts"):
            verdicts = {v["expected_verdict"] for v in self._of_gate(gate)}
            self.assertIn("accept", verdicts, f"{gate} has no accept control")
            self.assertIn("reject", verdicts, f"{gate} has no rejection vector")


if __name__ == "__main__":
    unittest.main()
