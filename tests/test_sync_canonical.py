"""The content_hash canonicalization in sync/_common.py follows
docs/federation.md exactly.

sync/ carries its own implementation because it may not import tools/ and the
CLI of tools/canonical.py is not yet fixed. This suite pins it to the anchor
value recorded in examples/valid-entry.yaml so the two cannot drift apart
unnoticed, and proves each canonicalization step by showing that the hash is
invariant under exactly the transformations the spec says are invisible.
"""

import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common  # noqa: E402

ANCHOR_PATH = synctest.REPO / "examples" / "valid-entry.yaml"


class CanonicalHash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = _common.load_yaml(ANCHOR_PATH)
        cls.entry = cls.doc["entries"][0]

    def test_matches_anchor_recorded_in_examples(self):
        self.assertEqual(self.entry["content_hash"], _common.content_hash(self.entry))

    def test_only_scope_and_rule_participate(self):
        e = copy.deepcopy(self.entry)
        e["status"] = "deprecated"
        e["slug"] = "renamed"
        e["lifecycle"]["updated_at"] = "2030-01-01"
        e["verification"]["last_verified_at"] = "2030-01-01"
        e["verification"]["verified_by"] = ["example-revalidator"]
        e["provenance"]["origin_repo"] = "someone-else/fork"
        e["provenance"]["redaction_profile"] = "r19"
        e["redaction_cleared_under"] = "r99"
        e["confidence"] = "low"
        self.assertEqual(self.entry["content_hash"], _common.content_hash(e))

    def test_fingerprints_are_case_whitespace_order_and_duplicate_insensitive(self):
        e = copy.deepcopy(self.entry)
        fps = list(reversed(e["rule"]["fingerprints"]))
        fps[0] = "  " + fps[0].upper().replace(" ", "   ") + " "
        fps.append(fps[1])  # duplicate
        fps.append("   ")  # empty after strip
        e["rule"]["fingerprints"] = fps
        self.assertEqual(self.entry["content_hash"], _common.content_hash(e))

    def test_prose_is_stripped_and_lf_normalized_but_not_reflowed(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "  " + e["rule"]["summary"] + "\r\n"
        self.assertEqual(self.entry["content_hash"], _common.content_hash(e))
        e["rule"]["summary"] = e["rule"]["summary"].strip().replace(" ", "  ", 1)
        self.assertNotEqual(self.entry["content_hash"], _common.content_hash(e), "reflowing prose is a change")

    def test_changing_the_claim_changes_the_hash(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["resolution"] += " Also restart the service."
        self.assertNotEqual(self.entry["content_hash"], _common.content_hash(e))
        e = copy.deepcopy(self.entry)
        e["scope"]["topology"] = {"values": ["tp8"]}
        self.assertNotEqual(self.entry["content_hash"], _common.content_hash(e))

    def test_serialization_is_compact_sorted_and_non_ascii_preserving(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "Ünïcode summary"
        s = _common.canonical_json(e)
        self.assertTrue(s.startswith('{"rule":{"avoidance":"'), s[:40])
        self.assertIn('"summary":"Ünïcode summary","symptom":"', s)
        self.assertIn('"],"resolution":"', s, "compact separators, keys sorted")
        self.assertIn('},"scope":{"cann":{', s)

    def test_nbsp_is_content_and_ascii_lower_is_ascii_only(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "\u00a0" + e["rule"]["summary"] + "\u3000"
        e["rule"]["fingerprints"] = ["ABC İ É Σ", "A\u00a0B"]
        payload = _common.canonical_payload(e)
        self.assertTrue(payload["rule"]["summary"].startswith("\u00a0"))
        self.assertEqual(
            ["abc İ É Σ", "a\u00a0b"],
            payload["rule"]["fingerprints"],
        )

    def test_per_line_trailing_ascii_whitespace_is_stripped_in_rule_and_scope(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "First line  \nSecond line"
        e["scope"]["soc"]["basis"] = "Synthetic first line\t \nsecond line"
        payload = _common.canonical_payload(e)
        self.assertEqual("First line\nSecond line", payload["rule"]["summary"])
        self.assertEqual("Synthetic first line\nsecond line", payload["scope"]["soc"]["basis"])

    def test_numeric_bound_is_rejected_not_stringified(self):
        e = copy.deepcopy(self.entry)
        e["scope"]["torch"]["range"]["min"] = 2.5
        with self.assertRaises(_common.SyncError) as ctx:
            _common.content_hash(e)
        self.assertIn("2.5", str(ctx.exception))

    def test_non_string_fingerprint_is_rejected_not_stringified(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["fingerprints"] = [123]
        with self.assertRaises(_common.SyncError) as ctx:
            _common.content_hash(e)
        self.assertIn("123", str(ctx.exception))

    def test_yaml_date_quoting_does_not_matter(self):
        # A producer that writes unquoted dates yields date objects on load;
        # the loader normalizes them so the hash and the schema see strings.
        text = ANCHOR_PATH.read_text().replace("'2026-09-07'", "2026-09-07")
        tmp = synctest.tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        self.addCleanup(pathlib.Path(tmp.name).unlink)
        tmp.write(text)
        tmp.close()
        doc = _common.load_yaml(pathlib.Path(tmp.name))
        self.assertEqual("2026-09-07", doc["updated_at"])
        self.assertIsInstance(doc["entries"][0]["lifecycle"]["first_seen"], str)


if __name__ == "__main__":
    unittest.main()
