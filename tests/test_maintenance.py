"""Readiness belongs to installation and MCP lifecycle, never a query."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vaws_knowledge.distribution.sync import SwitchLock
from vaws_knowledge.local.backend import MemoryBackend, UnavailableBackend
from vaws_knowledge.maintenance import MaintenanceWorker, main, maintain, project_config
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.server.query import query


class Maintenance(unittest.TestCase):
    def config(self, root):
        notes = root / "notes"
        notes.mkdir()
        (notes / "fact.md").write_text("# Recorded\n\nmaintenancecanary, still uncertain.\n", encoding="utf-8")
        config = load_config({"backend": "memory", "state_root": str(root / "state"),
                              "layers": {"shared": {"enabled": False}, "project": str(notes), "candidate": str(root / "candidate")},
                              "shared_sync": {"enabled": False}, "publishing": {"enabled": False}}, env={})
        config.retrieval = MemoryBackend()
        return config

    def test_local_readiness_does_not_require_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertEqual([], query(config, text="maintenancecanary").results)
            with patch("vaws_knowledge.publishing.run_once", return_value={"status": "disabled"}) as shared:
                result = maintain(config, verify=True)
            self.assertTrue(result["ready"], result)
            shared.assert_called_once_with(config, force=False, verify=True)
            self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_loss_is_repaired_and_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            original = (root / "notes" / "fact.md").read_bytes()
            with patch("vaws_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                maintain(config, verify=True)
                config.retrieval.documents.clear()
                repaired = maintain(config, verify=True)
            self.assertTrue(repaired["ready"], repaired)
            self.assertEqual(1, len(query(config, text="maintenancecanary").results))
            self.assertEqual(original, (root / "notes" / "fact.md").read_bytes())

    def test_down_engine_is_pending_and_retried_without_query_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            config.retrieval = UnavailableBackend("offline")
            self.assertFalse(maintain(config, verify=True)["ready"])
            with patch.object(config.retrieval, "available", side_effect=AssertionError("started inline")):
                self.assertTrue(query(config, text="maintenancecanary").unavailable)
            config.retrieval = MemoryBackend()
            with patch("vaws_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                self.assertTrue(maintain(config, verify=True)["ready"])

    def test_parallel_connection_does_not_run_a_second_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            lock = SwitchLock(config.state_root / "maintenance.lock")
            lock.acquire()
            try:
                with patch("vaws_knowledge.maintenance.reconcile_markdown") as reconcile:
                    self.assertEqual("busy", maintain(config)["status"])
                    reconcile.assert_not_called()
            finally:
                lock.release()

    def test_failed_shared_sync_keeps_local_retrieval_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("vaws_knowledge.publishing.run_once", return_value={"status": "partial", "sync": {"status": "offline"}}):
                result = maintain(config, verify=True)
            self.assertFalse(result["ready"])
            payload = query(config, text="maintenancecanary")
            self.assertTrue(payload.degraded)
            self.assertFalse(payload.unavailable)
            self.assertEqual(1, len(payload.results))

    def test_new_shared_model_rebuilds_local_vectors_before_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            model = {"revision": "before"}
            config.retrieval.index_fingerprint = lambda: dict(model)
            def import_new_model(*args, **kwargs):
                model["revision"] = "after"
                return {"status": "ok", "sync": {"status": "switched"}}
            with patch("vaws_knowledge.publishing.run_once", side_effect=import_new_model), \
                    patch.object(config.retrieval, "upsert", wraps=config.retrieval.upsert) as writes:
                result = maintain(config, verify=True)
            self.assertTrue(result["ready"], result)
            self.assertEqual(2, writes.call_count)
            ledger = json.loads((config.state_root / "markdown-index.json").read_text(encoding="utf-8"))
            self.assertEqual({"revision": "after"}, next(iter(ledger["documents"].values()))["index_fingerprint"])

    def test_worker_runs_even_when_contributions_are_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("vaws_knowledge.maintenance.maintain") as tick:
                worker = MaintenanceWorker(config)
                worker.start()
                worker.stop()
            tick.assert_called()
            self.assertEqual({"force": False}, tick.call_args.kwargs)

    def test_query_without_readiness_record_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(query(config, text="maintenancecanary").degraded)

    def test_new_connection_reuses_fresh_audit_but_expiry_repairs_index_loss(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("vaws_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                self.assertTrue(maintain(config, verify=True)["ready"])
                config.retrieval.documents.clear()
                worker = MaintenanceWorker(config)
                completed = threading.Event()
                def first_pass(*args, **kwargs):
                    result = maintain(*args, **kwargs)
                    worker.closed.set()
                    completed.set()
                    return result
                with patch("vaws_knowledge.maintenance.maintain", side_effect=first_pass):
                    worker.start()
                    self.assertTrue(completed.wait(3))
                    worker.stop()
                # Reconnection is no longer a forced full audit. The fresh
                # receipt remains reusable until its explicit audit deadline.
                self.assertEqual([], query(config, text="maintenancecanary").results)
                receipt = json.loads((config.state_root / "maintenance.json").read_text())
                with patch("vaws_knowledge.maintenance.time.time", return_value=receipt["next_verify"] + 1):
                    self.assertTrue(maintain(config)["ready"])
                self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_fresh_deadlines_skip_backend_work_and_expired_audit_overrides_next_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("vaws_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                result = maintain(config, verify=True)
                with patch("vaws_knowledge.maintenance.backend_for_config", side_effect=AssertionError("unneeded backend")):
                    self.assertEqual(result, maintain(config))
                # Even an inconsistent/future next_check cannot postpone an
                # already due explicit audit of the saved vectors.
                result["next_check"] = result["next_verify"] + 100
                (config.state_root / "maintenance.json").write_text(json.dumps(result))
                config.retrieval.documents.clear()
                with patch("vaws_knowledge.maintenance.time.time", return_value=result["next_verify"] + 1):
                    repaired = maintain(config)
                self.assertTrue(repaired["ready"])
                self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_project_prepare_preserves_config_and_does_not_enable_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = project_config(root)
            self.assertFalse(config.publishing["enabled"])
            self.assertTrue(config.shared_sync["enabled"])
            self.assertEqual(((root / ".agents" / "knowledge").resolve(),), config.mount("project").roots)
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            payload["publishing"] = {"enabled": True, "repository": "existing/repo", "fork": "existing/fork"}
            config.config_path.write_text(json.dumps(payload), encoding="utf-8")
            again = project_config(root)
            self.assertEqual(payload["publishing"], again.publishing)

    def test_prepare_cli_emits_readiness_without_starting_on_help(self):
        with patch("vaws_knowledge.maintenance.maintain") as work, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--help"])
            work.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            for ready in (False, True):
                output = io.StringIO()
                with patch("vaws_knowledge.maintenance.maintain", return_value={"ready": ready, "status": "ready" if ready else "pending"}), redirect_stdout(output):
                    rc = main(["--project", tmp])
                self.assertEqual(0 if ready else 1, rc)
                self.assertEqual(ready, json.loads(output.getvalue())["ready"])
