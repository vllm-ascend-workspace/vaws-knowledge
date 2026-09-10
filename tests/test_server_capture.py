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
