"""Sourced-reference body: hash, capture, query, export. Not runtime evidence."""
from __future__ import annotations

import copy
import pathlib
import tempfile
import unittest

from test_tools_support import EXAMPLE_ENTRY, ANCHOR_HASH, load_yaml, run_tool

from vaws_knowledge import canonical, validate
from vaws_knowledge._common import ToolError
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.query import query

REPO = pathlib.Path(__file__).resolve().parent.parent
REFERENCE_DOC = REPO / "corpus" / "unverified" / "sourced-references.yaml"
REFERENCE_HASH = "sha256:f69a469c6089c485cc94aa6ec1fe6741234dc3bee8798bd47ae223b23027475a"
REFERENCE_UUID = "7c2e9a10-4b3d-4f1a-8c6e-2a9b0d4e5f11"


class ReferenceCanonical(unittest.TestCase):
    def test_rule_anchor_hash_did_not_move(self):
        entry = load_yaml(EXAMPLE_ENTRY)["entries"][0]
        self.assertEqual(canonical.content_hash(entry), ANCHOR_HASH)
        payload = canonical.canonical_payload(entry)
        self.assertEqual(set(payload), {"rule", "scope"})

    def test_reference_hashes_body_only(self):
        entry = load_yaml(REFERENCE_DOC)["entries"][0]
        payload = canonical.canonical_payload(entry)
        self.assertEqual(set(payload), {"reference"})
        self.assertEqual(canonical.content_hash(entry), REFERENCE_HASH)
        self.assertEqual(entry["content_hash"], REFERENCE_HASH)

    def test_reference_with_scope_is_not_hashable(self):
        entry = load_yaml(REFERENCE_DOC)["entries"][0]
        entry["scope"] = load_yaml(EXAMPLE_ENTRY)["entries"][0]["scope"]
        with self.assertRaises(ToolError):
            canonical.content_hash(entry)


class ReferenceValidateExport(unittest.TestCase):
    def test_packaged_corpus_reference_validates(self):
        result = validate.validate_paths([str(REFERENCE_DOC)])
        self.assertEqual([], [p.render() for p in result.problems], REFERENCE_DOC)

    def test_export_round_trip_keeps_reference_body(self):
        proc = run_tool(
            "export",
            str(REFERENCE_DOC),
            "--kind",
            "sourced-references",
            "--origin-repo",
            "vllm-ascend-workspace/vaws-knowledge",
            "--layer",
            "unverified",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reference", proc.stdout)
        self.assertIn("official_documentation", proc.stdout)
        self.assertNotIn("\nscope:", proc.stdout)


class ReferenceCaptureQuery(unittest.TestCase):
    def test_capture_and_query_label_sourced_reference(self):
        import sys
        sys.path.insert(0, str(REPO / "tests" / "fixtures" / "server"))
        import support

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = support.build_config(shared=False, project=False, candidate=tmp.name)
        entry = copy.deepcopy(load_yaml(REFERENCE_DOC)["entries"][0])
        entry.pop("uuid", None)
        entry.pop("content_hash", None)
        entry.pop("provenance", None)
        entry.pop("lifecycle", None)
        result = capture(entry, kind="sourced-references", config=config)
        self.assertTrue(result["ok"])
        payload = query(config, text="newline-delimited JSON-RPC", include_unverified=True).to_dict()
        self.assertTrue(payload["results"])
        row = payload["results"][0]
        self.assertEqual(row["body"], "reference")
        self.assertEqual(row["evidence_class"], "sourced_reference")
        self.assertEqual(row["applicability"]["runtime_scoped"], False)
        self.assertNotEqual(row["body"], "rule")
        self.assertTrue(any("sourced reference material" in w for w in row["warnings"]))
        self.assertFalse(any("single unconfirmed observation" in w for w in row["warnings"]))

    def test_default_shared_query_includes_unverified_reference_not_unverified_facts(self):
        import sys
        sys.path.insert(0, str(REPO / "tests" / "fixtures" / "server"))
        import support

        config = support.build_config(
            shared=REPO / "corpus" / "unverified",
            project=False,
            candidate=False,
        )
        payload = query(config, text="newline-delimited JSON-RPC").to_dict()
        refs = [row for row in payload["results"] if row.get("body") == "reference"]
        self.assertTrue(refs, payload["results"])
        row = refs[0]
        self.assertEqual(row["evidence_class"], "sourced_reference")
        self.assertEqual(row["status"], "unverified")
        self.assertTrue(any("sourced reference material" in w for w in row["warnings"]))
        self.assertFalse(any("single unconfirmed observation" in w for w in row["warnings"]))
        self.assertFalse(any(row.get("body") == "measurement" for row in payload["results"]))
