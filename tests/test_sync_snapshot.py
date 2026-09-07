"""Verified-only snapshot wrapper: schema + redaction gates, then publish.py."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common  # noqa: E402

sys.path.insert(0, str(synctest.SYNC_DIR))
import snapshot as snapshot_mod  # noqa: E402


class SnapshotWrapper(synctest.SyncTestCase):
    def _run(self, out, **kwargs):
        kwargs.setdefault("revision", "0123abcd")
        kwargs.setdefault("generated_at", "2026-09-10T00:00:00+00:00")
        return snapshot_mod.run_verified_snapshot(
            corpus_dir=self.corpus_dir,
            tools_dir=synctest.REPO / "tools",
            out=out,
            **kwargs,
        )

    def test_verified_only_manifest_and_digest(self):
        out = self.tmp / "snap"
        result = self._run(out)
        self.assertEqual("published", result["status"], result)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("verified", manifest["layer"])
        self.assertEqual(1, manifest["snapshot_format"])
        self.assertTrue(str(manifest["snapshot_digest"]).startswith("sha256:"))
        self.assertEqual(manifest["snapshot_digest"], result["snapshot_digest"])
        self.assertEqual("0123abcd", manifest["corpus_revision"])
        text = (out / "verified" / "known-failure-signatures.yaml").read_text(encoding="utf-8")
        self.assertIn(synctest.UUID_VERIFIED, text)
        self.assertNotIn(synctest.UUID_UNVERIFIED, text)
        self.assertFalse((out / "unverified").exists())

    def test_empty_verified_corpus_is_labelled_empty(self):
        for path in (self.corpus_dir / "verified").glob("*.yaml"):
            path.unlink()
        out = self.tmp / "empty-snap"
        result = self._run(out)
        self.assertEqual("published", result["status"], result)
        self.assertTrue(result["empty"])
        self.assertEqual(0, result["entry_count"])
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(0, manifest["entry_count"])
        self.assertEqual("verified", manifest["layer"])
        self.assertTrue(str(manifest["snapshot_digest"]).startswith("sha256:"))

    def test_redaction_failure_writes_nothing(self):
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(path)
        doc["entries"][0]["rule"]["resolution"] += " contact synthetic-private-person@corp.invalid"
        path.write_text(_common.dump_yaml(doc), encoding="utf-8")
        out = self.tmp / "blocked-snap"
        result = self._run(out)
        self.assertEqual("failed", result["status"])
        self.assertFalse(result["wrote"])
        self.assertFalse((out / "manifest.json").exists())

    def test_schema_failure_writes_nothing(self):
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        path.write_text("schema_version: 1\nentries: []\n", encoding="utf-8")
        out = self.tmp / "schema-snap"
        with self.assertRaises(_common.SyncError):
            self._run(out)
        self.assertFalse((out / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
