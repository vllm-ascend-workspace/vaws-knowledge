"""Markdown capture: title+content only, candidate layer only."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.local.backend import MemoryBackend
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
