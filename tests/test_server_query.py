"""OpenViking-backed query contract without the YAML scoring engine."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.local.backend import MemoryBackend, UnavailableBackend
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.query import explain, query


def _config(tmp: str):
    config = support.build_config(candidate=tmp, shared=False, project=False)
    config.retrieval = MemoryBackend()
    return config


class QueryMarkdown(unittest.TestCase):
    def test_unknown_conditions_are_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            capture(
                title="graph replay mismatch",
                content="Eager passed; graph replay diverged on padding.",
                config=config,
            )
            payload = query(
                config,
                text="graph replay",
                conditions={"soc": "Ascend910B4"},
            ).to_dict()
            self.assertEqual(1, payload["count"])
            self.assertTrue(payload["results"][0]["applies"])

    def test_known_mismatch_is_filtered_after_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            capture(
                title="910B note",
                content="Only seen on 910B graph replay.",
                conditions={"soc": "Ascend910B4"},
                config=config,
            )
            dropped = query(
                config,
                text="graph replay",
                conditions={"soc": "Ascend310P"},
            ).to_dict()
            self.assertEqual([], dropped["results"])
            self.assertGreaterEqual(dropped["filtered_before_limit"], 1)
            kept = query(
                config,
                text="graph replay",
                conditions={"soc": "Ascend310P"},
                include_non_matching=True,
            ).to_dict()
            self.assertEqual(1, kept["count"])
            self.assertFalse(kept["results"][0]["applies"])

    def test_unavailable_index_is_unknown_not_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            config.retrieval = UnavailableBackend("down")
            payload = query(config, text="anything").to_dict()
            self.assertTrue(payload["unavailable"])
            self.assertTrue(payload["degraded"])
            self.assertEqual([], payload["results"])
            self.assertIn("UNKNOWN", payload["no_result_meaning"])

    def test_explain_returns_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            saved = capture(
                title="explain me",
                content="full body of the note",
                config=config,
            )
            payload = explain(config, saved["uri"])
            self.assertTrue(payload["found"])
            self.assertEqual("full body of the note", payload["content"])

    def test_explain_missing_ref_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = explain(config, "missing-ref")
            self.assertFalse(payload["found"])
            self.assertIn("unknown", payload["meaning"].lower())

    def test_local_and_public_are_reference_and_not_ranked_by_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = pathlib.Path(tmp) / "candidate"
            project = pathlib.Path(tmp) / "project"
            candidate.mkdir()
            project.mkdir()
            config = support.build_config(candidate=str(candidate), project=str(project), shared=False)
            config.retrieval = MemoryBackend()
            capture(
                title="local graph replay note",
                content="Candidate observation about graph replay padding.",
                config=config,
            )
            from vaws_knowledge.markdown import save_document, slugify, uri_for

            saved = save_document(
                project,
                layer="project",
                title="project graph replay note",
                content="Project observation about graph replay padding.",
                status="verified",
            )
            config.retrieval.upsert(
                saved.uri or uri_for("project", slugify("project graph replay note")),
                saved.path.read_text(encoding="utf-8"),
                layer="project",
            )
            payload = query(config, text="graph replay padding").to_dict()
            layers = {item["layer"] for item in payload["results"]}
            self.assertIn("candidate", layers)
            self.assertIn("project", layers)
            self.assertTrue(all(item["role"] == "reference" for item in payload["results"]))
            self.assertTrue(any("reference" in note.lower() for note in payload["notes"]))
