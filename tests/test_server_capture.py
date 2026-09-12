"""Markdown capture: title+content only, candidate layer only."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.local.backend import MemoryBackend
from vaws_knowledge.markdown import meta_path
from vaws_knowledge.server.capture import CaptureRefused, CaptureRejected, capture, delete
from vaws_knowledge.server.query import query


def _config(tmp: str):
    config = support.build_config(candidate=tmp)
    config.retrieval = MemoryBackend()
    return config


class CaptureMarkdown(unittest.TestCase):
    def test_other_kind_alias_cannot_be_found_updated_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(str(pathlib.Path(tmp) / "candidate"))
            experience = config.for_kind("experience")
            observed = capture(title="Graph case", content="Original historical failure.", config=experience)
            root = config.mount("candidate").roots[0]
            root.mkdir(parents=True, exist_ok=True)
            alias = root / "alias.md"
            alias.symlink_to(observed["path"])
            self.assertEqual([], query(config, text="historical failure", layers=["candidate"]).results)
            with self.assertRaises(CaptureRejected):
                delete("alias.md", config=config)
            current = capture(title="Graph case", content="Current reference.", config=config)
            self.assertNotEqual(str(alias), current["path"])
            self.assertIn("Original historical", pathlib.Path(observed["path"]).read_text())

    def test_same_title_in_each_kind_is_stored_and_deleted_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(str(pathlib.Path(tmp) / "candidate"))
            experience = config.for_kind("experience")
            current = capture(title="Graph metadata", content="Current allocation contract.", config=config)
            observed = capture(title="Graph metadata", content="Observed a failure on an old revision.", config=experience)
            corrected = capture(title="Graph metadata", content="Corrected the earlier causal interpretation.", config=experience)
            self.assertNotEqual(current["path"], observed["path"])
            self.assertEqual(observed["path"], corrected["path"])
            self.assertIn("Current allocation", pathlib.Path(current["path"]).read_text())
            self.assertEqual("experience", corrected["kind"])
            with self.assertRaises(CaptureRejected):
                delete(observed["uri"], config=config)
            delete(current["uri"], config=config)
            self.assertFalse(pathlib.Path(current["path"]).exists())
            self.assertTrue(pathlib.Path(observed["path"]).is_file())
            self.assertIn(observed["uri"], config.retrieval.documents)

    def test_title_and_content_are_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = capture(
                title="一次图模式启动失败的排查经验",
                content="当时遇到了启动失败。检查后发现 metadata 未复用，修复后启动成功。",
                config=config,
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["layer"], "candidate")
            self.assertTrue(pathlib.Path(payload["path"]).is_file())
            self.assertIn("metadata", pathlib.Path(payload["path"]).read_text(encoding="utf-8"))
            found = query(config, text="图模式启动失败").to_dict()
            self.assertEqual(1, found["count"])
            self.assertEqual(payload["uri"], found["results"][0]["uri"])

    def test_unknown_conditions_are_omitted_known_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = capture(
                title="HCCL hang",
                content="Collective stalled after rank map mismatch.",
                conditions={"soc": "Ascend910B4", "cann": "unknown", "vllm": ""},
                source={"session": "abc"},
                config=config,
            )
            document = payload["document"]
            self.assertEqual({"soc": "Ascend910B4"}, document["conditions"])
            self.assertEqual({"session": "abc"}, document["source"])

    def test_missing_title_or_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            with self.assertRaises(CaptureRejected):
                capture(title="", content="body", config=config)
            with self.assertRaises(CaptureRejected):
                capture(title="title", content="  ", config=config)

    def test_non_candidate_layer_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            for layer in ("shared", "project", "verified"):
                with self.assertRaises(CaptureRefused):
                    capture(title="x", content="y", layer=layer, config=config)

    def test_read_only_candidate_is_not_written(self) -> None:
        from vaws_knowledge.server.layers import load_config
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "read-only"
            config = load_config({"layers": {"candidate": {"root": str(target), "read_only": True}}}, env={})
            with self.assertRaises(CaptureRefused):
                capture(title="Reference", content="Must not write here.", config=config, index=False)
            self.assertFalse(target.exists())

    def test_delete_removes_file_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = capture(title="temp note", content="delete me please", config=config)
            delete(payload["uri"], config=config)
            self.assertFalse(pathlib.Path(payload["path"]).exists())
            found = query(config, text="delete me please").to_dict()
            self.assertEqual([], found["results"])

    def test_distinct_chinese_titles_keep_separate_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            first = capture(title="图模式失败", content="graph compile failed", config=config)
            second = capture(title="通信超时", content="rpc timed out", config=config)
            updated = capture(title="图模式失败", content="graph compile failed again", config=config)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(first["path"], updated["path"])
            self.assertTrue(pathlib.Path(first["path"]).is_file())
            self.assertIn("图模式失败", pathlib.Path(first["path"]).read_text(encoding="utf-8"))
            self.assertIn("again", pathlib.Path(first["path"]).read_text(encoding="utf-8"))
            self.assertIn("通信超时", pathlib.Path(second["path"]).read_text(encoding="utf-8"))
            self.assertEqual("图模式失败", query(config, text="graph compile").to_dict()["results"][0]["title"])

    def test_dry_run_does_not_write_or_delete_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            original = capture(
                title="dry run existing",
                content="keep this original text",
                conditions={"soc": "Ascend910B4"},
                source={"session": "s1"},
                config=config,
            )
            path = pathlib.Path(original["path"])
            sidecar = meta_path(path)
            before_md = path.read_bytes()
            before_meta = sidecar.read_bytes()
            indexed_before = dict(config.retrieval.documents)

            with mock.patch("vaws_knowledge.server.capture.backend_for_config") as backend_factory:
                proposed = capture(
                    title="dry run existing",
                    content="replacement must not land",
                    dry_run=True,
                    config=config,
                )
                backend_factory.assert_not_called()

            self.assertTrue(proposed["ok"])
            self.assertTrue(proposed["dry_run"])
            self.assertEqual("skipped", proposed["index"])
            self.assertTrue(proposed["would_update"])
            self.assertEqual(before_md, path.read_bytes())
            self.assertEqual(before_meta, sidecar.read_bytes())
            self.assertEqual(indexed_before, config.retrieval.documents)

            fresh = capture(title="brand new dry run", content="never write this", dry_run=True, config=config)
            self.assertFalse(pathlib.Path(fresh["path"]).exists())
            self.assertFalse(fresh["would_update"])
            self.assertEqual([path], list(pathlib.Path(tmp).glob("*.md")))
