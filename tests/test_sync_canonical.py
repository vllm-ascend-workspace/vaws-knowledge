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
        e["provenance"]["origin_repo"] = "someone-else/fork"
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
