"""Live OpenViking loopback path. Skips when the engine is not installed."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from vaws_knowledge.server.capture import capture, delete
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.server.query import explain, query


def _openviking_ready() -> bool:
    if os.environ.get("VAWS_KNOWLEDGE_LIVE_OV") == "0":
        return False
    try:
        import openviking_sdk  # noqa: F401
    except ImportError:
        return False
    from vaws_knowledge.local.instance import which

    return which("openviking-server") is not None


@unittest.skipUnless(_openviking_ready(), "openviking-server is not installed")
class LiveOpenViking(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        candidate = root / "candidate"
        candidate.mkdir()
        state = root / "instance"
        self.config = load_config(
            {
                "backend": "openviking",
                "state_root": str(state),
                "layers": {
                    "shared": {"enabled": False},
                    "project": {"enabled": False},
                    "candidate": {"root": str(candidate)},
                },
            },
            env={},
            base_dir=root,
        )

    def tearDown(self) -> None:
        from vaws_knowledge.local.instance import instance_for_config

        try:
            instance_for_config(self.config).stop()
        except Exception:  # noqa: BLE001
            pass

    def test_capture_query_update_delete_and_restart(self) -> None:
        first = capture(
            title="graph replay mismatch",
            content="Eager passed but ACL graph replay diverged on padding metadata.",
            config=self.config,
        )
        self.assertTrue(first["ok"])
        found = query(self.config, text="graph replay padding").to_dict()
        self.assertGreaterEqual(found["count"], 1, found)
        self.assertFalse(found.get("unavailable"))
        body = explain(self.config, first["uri"])
        self.assertTrue(body["found"])
        self.assertIn("padding metadata", body["content"])

        updated = capture(
            title="graph replay mismatch",
            content="Updated: the mismatch was a reused slot mapping, not padding.",
            config=self.config,
        )
        self.assertEqual(first["slug"], updated["slug"])
        again = explain(self.config, updated["uri"])
        self.assertIn("slot mapping", again["content"])

        from vaws_knowledge.local.instance import instance_for_config

        instance_for_config(self.config).stop()
        restarted = query(self.config, text="slot mapping").to_dict()
        self.assertGreaterEqual(restarted["count"], 1, restarted)

        delete(updated["uri"], config=self.config)
        missing = query(self.config, text="slot mapping").to_dict()
        self.assertEqual([], missing["results"])
