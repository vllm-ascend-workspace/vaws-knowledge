"""sync/publish.py: the verified snapshot is reproducible and self-describing."""

import json
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common, publish_mod  # noqa: E402


class BuildSnapshot(synctest.SyncTestCase):
    def _build(self, **kwargs):
        kwargs.setdefault("revision", "0123abcd")
        kwargs.setdefault("generated_at", "2026-09-10T00:00:00+00:00")
        return publish_mod.build_snapshot(self.corpus(), **kwargs)

    def test_byte_reproducible(self):
        first = self._build()
        # Perturb nothing semantic: reorder entries and keys on disk, re-load.
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(path)
        doc["entries"].reverse()
        doc["entries"][0] = dict(reversed(list(doc["entries"][0].items())))
        path.write_text(synctest.yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
        second = self._build()
        self.assertEqual(first, second)

    def test_manifest_describes_the_snapshot(self):
        files = self._build()
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(1, manifest["snapshot_format"])
        self.assertEqual("verified", manifest["layer"])
        self.assertEqual("0123abcd", manifest["corpus_revision"])
        self.assertEqual("2026-09-10T00:00:00+00:00", manifest["generated_at"])
        self.assertEqual(2, manifest["entry_count"])
        self.assertEqual("r1", manifest["redaction_profile_floor"])
        self.assertEqual({"r1": 2}, manifest["redaction_profiles"])
        self.assertEqual({"verified": 2}, manifest["status_counts"])
        self.assertEqual(["verified/known-failure-signatures.yaml"], [k["file"] for k in manifest["kinds"]])
        self.assertTrue(manifest["snapshot_digest"].startswith("sha256:"))

    def test_floor_is_the_weakest_profile_present(self):
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(path)
        doc["entries"][0]["provenance"]["redaction_profile"] = "r3"
        doc["entries"][1]["provenance"]["redaction_profile"] = "r12"
        path.write_text(_common.dump_yaml(doc), encoding="utf-8")
        manifest = json.loads(self._build()["manifest.json"])
        self.assertEqual("r3", manifest["redaction_profile_floor"], "numeric, not lexical")

    def test_only_the_verified_layer_is_published(self):
        files = self._build()
        text = files["verified/known-failure-signatures.yaml"].decode()
        self.assertIn(synctest.UUID_VERIFIED, text)
        self.assertNotIn(synctest.UUID_UNVERIFIED, text)
        self.assertEqual({"manifest.json", "verified/known-failure-signatures.yaml"}, set(files))

    def test_refuses_unverified_status_inside_verified(self):
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(path)
        doc["entries"][0]["status"] = "unverified"
        path.write_text(_common.dump_yaml(doc), encoding="utf-8")
        with self.assertRaises(_common.IntegrityError):
            self._build()

    def test_refuses_stale_content_hash(self):
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(path)
        doc["entries"][0]["rule"]["summary"] += " edited without rehash"
        path.write_text(_common.dump_yaml(doc), encoding="utf-8")
        with self.assertRaises(_common.IntegrityError):
            self._build()

    def test_published_documents_are_loadable_v2_documents(self):
        files = self._build()
        doc = synctest.yaml.safe_load(files["verified/known-failure-signatures.yaml"])
        self.assertEqual(2, doc["schema_version"])
        self.assertEqual("verified", doc["layer"])
        self.assertEqual([synctest.UUID_VERIFIED, synctest.UUID_MARKER], [e["uuid"] for e in doc["entries"]])


class PublishCli(synctest.SyncTestCase):
    def _run(self, out, *extra, tools=synctest.TOOLS_PASS):
        cmd = [sys.executable, str(synctest.SYNC_DIR / "publish.py"), "--corpus", str(self.corpus_dir),
               "--tools-dir", str(tools), "--out", str(out), *extra]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_two_runs_are_byte_identical_and_touch_only_out(self):
        before = synctest.dir_digest(self.corpus_dir)
        a, b = self.tmp / "snap-a", self.tmp / "snap-b"
        for out in (a, b):
            proc = self._run(out, "--revision", "deadbeef", "--generated-at", "2026-09-10T00:00:00+00:00")
            self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(synctest.dir_digest(a), synctest.dir_digest(b))
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir), "publish never writes into the corpus")

    def test_source_date_epoch_pins_the_timestamp_outside_git(self):
        proc = subprocess.run(
            [sys.executable, str(synctest.SYNC_DIR / "publish.py"), "--corpus", str(self.corpus_dir),
             "--tools-dir", str(synctest.TOOLS_PASS), "--out", str(self.tmp / "snap"), "--revision", "x"],
            capture_output=True, text=True, env={"PATH": "", "SOURCE_DATE_EPOCH": "1800000000"},
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        manifest = json.loads((self.tmp / "snap" / "manifest.json").read_text())
        self.assertEqual("2027-01-15T08:00:00+00:00", manifest["generated_at"])

    def test_missing_validator_fails_closed(self):
        proc = self._run(self.tmp / "snap", "--revision", "x", "--generated-at", "2026-09-10T00:00:00+00:00",
                         tools=synctest.TOOLS_MISSING)
        self.assertEqual(_common.EXIT_GATE, proc.returncode)
        self.assertFalse((self.tmp / "snap").exists())

    def test_non_git_corpus_without_pins_is_refused(self):
        proc = subprocess.run(
            [sys.executable, str(synctest.SYNC_DIR / "publish.py"), "--corpus", str(self.corpus_dir),
             "--tools-dir", str(synctest.TOOLS_PASS), "--out", str(self.tmp / "snap"), "--revision", "x"],
            capture_output=True, text=True, env={"PATH": ""},
        )
        self.assertEqual(_common.EXIT_ERROR, proc.returncode)
        self.assertIn("--generated-at", proc.stderr)


if __name__ == "__main__":
    unittest.main()
