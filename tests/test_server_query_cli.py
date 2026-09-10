"""CLI for query/explain over Markdown."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.local.backend import MemoryBackend
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.query import main, reader_coordinate_from_args


def _run_cli(*argv: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    text = out.getvalue().strip()
    payload = json.loads(text) if text else {}
    return code, payload, err.getvalue()


class QueryCli(unittest.TestCase):
    def test_text_query_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = support.build_config(candidate=tmp, shared=False, project=False)
            config.retrieval = MemoryBackend()
            capture(title="cli note", content="hostname missing from etc hosts", config=config)
            cfg = pathlib.Path(tmp) / "cfg.json"
            cfg.write_text("{}", encoding="utf-8")
            with mock.patch("vaws_knowledge.server.layers.load_config", return_value=config):
                code, payload, _err = _run_cli("--config", str(cfg), "--text", "hostname missing")
            self.assertEqual(0, code, payload)
            self.assertGreaterEqual(payload.get("count", 0), 1)

    def test_text_required(self) -> None:
        code, _payload, err = _run_cli()
        self.assertEqual(2, code)
        self.assertIn("--text", err)

    def test_reader_flags_drop_unknown(self) -> None:
        args = type("A", (), {"soc": "unknown", "cann": "8.2.RC1", "torch": None})()
        for name in (
            "driver",
            "python_abi",
            "torch_npu",
            "vllm",
            "vllm_ascend",
            "model",
            "topology",
            "execution_mode",
            "component",
        ):
            setattr(args, name, None)
        self.assertEqual({"cann": "8.2.RC1"}, reader_coordinate_from_args(args))
