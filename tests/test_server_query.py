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

    def test_mounted_project_markdown_is_indexed_without_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            project = root / "project"
            candidate = root / "candidate"
            project.mkdir()
            candidate.mkdir()
            note = project / "project-note.md"
            note.write_text(
                "# Project graph knowledge\n\nProject graph padding uses a unique canary named projectquartz.\n",
                encoding="utf-8",
            )
            config = support.build_config(
                candidate=str(candidate), project=str(project), shared=False
            )
            config.retrieval = MemoryBackend()
            payload = query(
                config, text="project graph padding projectquartz", layers=["project"]
            ).to_dict()
            self.assertEqual(1, payload["count"], payload)
            self.assertFalse(payload["unavailable"])
            self.assertTrue(explain(config, str(note), layers=["project"])["found"])
            again = query(
                config, text="project graph padding projectquartz", layers=["project"]
            ).to_dict()
            self.assertEqual(1, again["count"])
            self.assertEqual(1, len(config.retrieval.documents))

    def test_deleted_markdown_is_dropped_from_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            saved = capture(
                title="candidate control",
                content="Candidate graph padding canary named candidateamber.",
                config=config,
            )
            pathlib.Path(saved["path"]).unlink()
            after = query(config, text="candidate graph padding", layers=["candidate"]).to_dict()
            self.assertEqual(0, after["count"], after)

    def test_pending_capture_is_indexed_once_the_engine_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            config.retrieval = UnavailableBackend("down")
            pending = capture(
                title="offline pending note",
                content="Written while the index was down, unique token pendingonyx.",
                config=config,
            )
            self.assertEqual("pending", pending["index"])
            self.assertTrue(pathlib.Path(pending["path"]).is_file())
            config.retrieval = MemoryBackend()
            found = query(config, text="pendingonyx").to_dict()
            self.assertEqual(1, found["count"], found)
            self.assertFalse(found["unavailable"])

    def test_reconcile_failure_keeps_markdown_and_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            project = root / "project"
            candidate = root / "candidate"
            project.mkdir()
            candidate.mkdir()
            path = project / "keep-me.md"
            path.write_text("# Keep me\n\nunique token keepmequartz\n", encoding="utf-8")
            config = support.build_config(
                candidate=str(candidate), project=str(project), shared=False
            )

            class Boom(MemoryBackend):
                def upsert(self, uri, content, *, layer, wait=True):
                    del uri, content, layer, wait
                    raise RuntimeError("embed failed")

            config.retrieval = Boom()
            payload = query(config, text="keepmequartz", layers=["project"]).to_dict()
            self.assertTrue(path.is_file())
            self.assertTrue(payload["degraded"])
            self.assertEqual([], payload["results"])

    def test_shared_markdown_is_not_swept_into_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            shared = root / "shared"
            project = root / "project"
            candidate = root / "candidate"
            shared.mkdir()
            project.mkdir()
            candidate.mkdir()
            (shared / "public.md").write_text(
                "# Shared only\n\nunique token sharedonyx\n", encoding="utf-8"
            )
            config = support.build_config(
                shared=str(shared), project=str(project), candidate=str(candidate)
            )
            backend = MemoryBackend()
            upserts: list[str] = []
            original = backend.upsert

            def tracking(uri, content, *, layer, wait=True):
                upserts.append(uri)
                return original(uri, content, layer=layer, wait=wait)

            backend.upsert = tracking  # type: ignore[method-assign]
            config.retrieval = backend
            payload = query(config, text="sharedonyx", layers=["shared"]).to_dict()
            self.assertEqual([], upserts)
            self.assertEqual(0, payload["count"])
            self.assertNotIn("viking://resources/shared/public.md", backend.documents)
