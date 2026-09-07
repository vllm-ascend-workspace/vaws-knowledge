"""content_hash canonicalization (docs/federation.md)."""

from __future__ import annotations

import copy
import json
import unittest

from test_tools_support import (
    ANCHOR_HASH,
    EXAMPLE_ENTRY,
    TempDir,
    load_yaml,
    run_tool,
    write_yaml,
)

from tools import canonical
from tools._common import ToolError


class AnchorTests(unittest.TestCase):
    """The example entry is the cross-implementation anchor. If this fails,
    either the canonicalization drifted or examples/valid-entry.yaml changed
    without recomputing its hash. Neither is acceptable silently."""

    def setUp(self):
        self.entry = load_yaml(EXAMPLE_ENTRY)["entries"][0]

    def test_anchor_hash(self):
        self.assertEqual(canonical.content_hash(self.entry), ANCHOR_HASH)
        self.assertEqual(self.entry["content_hash"], ANCHOR_HASH)

    def test_cli_prints_anchor(self):
        proc = run_tool("canonical", str(EXAMPLE_ENTRY))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ANCHOR_HASH, proc.stdout)
        self.assertIn(self.entry["uuid"], proc.stdout)

    def test_cli_rejects_a_numeric_bound_before_printing_a_hash(self):
        doc = load_yaml(EXAMPLE_ENTRY)
        doc["entries"][0]["scope"]["torch"]["range"]["min"] = 2.5
        with TempDir() as tmp:
            path = write_yaml(tmp / "float-min.yaml", doc)
            proc = run_tool("canonical", str(path))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("sha256:", proc.stdout)
        self.assertIn("2.5", proc.stderr)


class CanonicalizationRules(unittest.TestCase):
    def setUp(self):
        self.entry = load_yaml(EXAMPLE_ENTRY)["entries"][0]

    def test_payload_contains_only_scope_and_rule(self):
        payload = canonical.canonical_payload(self.entry)
        self.assertEqual(set(payload), {"rule", "scope"})

    def test_processing_fields_do_not_change_hash(self):
        e = copy.deepcopy(self.entry)
        e["status"] = "stale"
        e["confidence"] = "low"
        e["slug"] = "renamed-slug"
        e["provenance"]["contributor"] = "someone-else"
        e["provenance"]["redaction_profile"] = "r19"
        e["redaction_cleared_under"] = "r99"
        e["lifecycle"]["updated_at"] = "2031-01-01"
        e["verification"]["last_verified_at"] = "2031-01-01"
        e["verification"]["verified_by"] = ["example-revalidator"]
        self.assertEqual(canonical.content_hash(e), ANCHOR_HASH)

    def test_claim_change_changes_hash(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] += " (reworded)"
        self.assertNotEqual(canonical.content_hash(e), ANCHOR_HASH)
        e = copy.deepcopy(self.entry)
        e["scope"]["topology"]["values"].append("tp32")
        self.assertNotEqual(canonical.content_hash(e), ANCHOR_HASH)

    def test_fingerprints_normalized(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["fingerprints"] = [
            "  Torch.Distributed   gloo init FAILED container ",
            "gloo makedeviceforhostname",
            "GLOO MAKEDEVICEFORHOSTNAME",  # duplicate after lowercasing
            "",
            "   ",
            "name or\tservice not known hostname",
        ]
        self.assertEqual(canonical.content_hash(e), ANCHOR_HASH)
        payload = canonical.canonical_payload(e)
        self.assertEqual(
            payload["rule"]["fingerprints"],
            [
                "gloo makedeviceforhostname",
                "name or service not known hostname",
                "torch.distributed gloo init failed container",
            ],
        )

    def test_other_strings_only_stripped_and_lf_normalized(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "  " + self.entry["rule"]["summary"] + "\n"
        e["scope"]["soc"]["basis"] = self.entry["scope"]["soc"]["basis"].replace(" ", " ")  # unchanged
        self.assertEqual(canonical.content_hash(e), ANCHOR_HASH)
        # Internal whitespace in prose is *not* collapsed.
        e["rule"]["summary"] = self.entry["rule"]["summary"].replace(" ", "  ", 1)
        self.assertNotEqual(canonical.content_hash(e), ANCHOR_HASH)
        # CRLF and CR become LF.
        e = copy.deepcopy(self.entry)
        e["rule"]["symptom"] = "line one\r\nline two"
        e2 = copy.deepcopy(self.entry)
        e2["rule"]["symptom"] = "line one\nline two"
        self.assertEqual(canonical.content_hash(e), canonical.content_hash(e2))

    def test_serialization_format(self):
        text = canonical.canonical_json(self.entry)
        self.assertTrue(text.startswith('{"rule":{'))
        # separators (",", ":"): no whitespace around structural characters.
        self.assertIn('"fingerprints":["gloo makedeviceforhostname","name or service', text)
        self.assertIn('},"scope":{"cann":{"any":true,"basis":', text)
        # sort_keys: "rule" precedes "scope"; inside rule, keys sorted.
        rule_keys = list(json.loads(text)["rule"])
        self.assertEqual(rule_keys, sorted(rule_keys))
        # ensure_ascii=False keeps non-ASCII intact.
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "größe ünïcode"
        self.assertIn("größe ünïcode", canonical.canonical_json(e))

    def test_null_and_bool_preserved(self):
        payload = canonical.canonical_payload(self.entry)
        self.assertIsNone(payload["scope"]["torch"]["range"]["max"])
        self.assertIs(payload["scope"]["soc"]["any"], True)

    def test_missing_scope_or_rule_is_an_error(self):
        e = copy.deepcopy(self.entry)
        del e["scope"]
        with self.assertRaises(ToolError):
            canonical.content_hash(e)
        with self.assertRaises(ToolError):
            canonical.content_hash({"rule": {}})

    def test_nbsp_and_ideographic_space_are_preserved(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "\u00a0" + e["rule"]["summary"] + "\u3000"
        e["rule"]["fingerprints"] = ["A\u00a0B"]
        payload = canonical.canonical_payload(e)
        self.assertTrue(payload["rule"]["summary"].startswith("\u00a0"))
        self.assertTrue(payload["rule"]["summary"].endswith("\u3000"))
        self.assertEqual(["a\u00a0b"], payload["rule"]["fingerprints"])

    def test_ascii_lowercase_preserves_non_ascii_letters(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["fingerprints"] = ["ABC İ É Σ"]
        self.assertEqual(
            ["abc İ É Σ"], canonical.canonical_payload(e)["rule"]["fingerprints"]
        )

    def test_interior_line_trailing_ascii_whitespace_is_stripped(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["summary"] = "First line  \nSecond line"
        e["scope"]["soc"]["basis"] = "Synthetic first line\t \nsecond line"
        payload = canonical.canonical_payload(e)
        self.assertEqual("First line\nSecond line", payload["rule"]["summary"])
        self.assertEqual("Synthetic first line\nsecond line", payload["scope"]["soc"]["basis"])

    def test_numeric_bound_is_rejected_not_stringified(self):
        e = copy.deepcopy(self.entry)
        e["scope"]["torch"]["range"]["min"] = 2.5
        with self.assertRaises(ToolError) as ctx:
            canonical.content_hash(e)
        self.assertIn("2.5", str(ctx.exception))
        self.assertIn("stringify", str(ctx.exception))

    def test_non_string_fingerprint_is_rejected_not_stringified(self):
        e = copy.deepcopy(self.entry)
        e["rule"]["fingerprints"] = [123]
        with self.assertRaises(ToolError) as ctx:
            canonical.content_hash(e)
        self.assertIn("123", str(ctx.exception))

    def test_non_string_mapping_key_is_rejected_not_stringified(self):
        e = copy.deepcopy(self.entry)
        e["rule"][1] = "nope"
        with self.assertRaises(ToolError) as ctx:
            canonical.content_hash(e)
        self.assertIn("stringify", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
