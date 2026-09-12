"""Prepare retrieval once, then quietly maintain it while MCP is running.

This is package work, independent of public contribution permission. Markdown
and verified release packs are the recoverable sources; the index is derived.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from vaws_knowledge.distribution.errors import SwitchInProgress
from vaws_knowledge.distribution.manifest import atomic_write_json, read_json
from vaws_knowledge.distribution.sync import SwitchLock
from vaws_knowledge.local.backend import backend_for_config
from vaws_knowledge.local.instance import instance_for_config
from vaws_knowledge.local.reconcile import reconcile_markdown
from vaws_knowledge.server.layers import ServiceConfig, load_config

POLL_SECONDS = 10
VERIFY_SECONDS = 3600


def maintenance_status(config: ServiceConfig) -> dict[str, Any]:
    return read_json(instance_for_config(config).state_root / "maintenance.json") or {}


def maintain(config: ServiceConfig, *, verify: bool = False, force: bool = False) -> dict[str, Any]:
    """Run one bounded pass. Process ownership prevents concurrent repairs."""
    root = instance_for_config(config).state_root
    lock = SwitchLock(root / "maintenance.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        previous = maintenance_status(config)
        return {**previous, "status": "busy", "ready": bool(previous.get("ready"))}
    try:
        previous = maintenance_status(config)
        now = time.time()
        if not force and not verify and now < previous.get("next_check", 0):
            return previous
        audit = verify or now >= previous.get("next_verify", 0)
        result: dict[str, Any] = {
            "status": "pending", "ready": False, "checked_at": now,
            "next_check": now + POLL_SECONDS,
            "next_verify": previous.get("next_verify", 0),
        }
        try:
            backend = backend_for_config(config)
            if backend.name == "openviking":
                backend.instance.ensure(verify_model=audit)
            model_before = dict(backend.index_fingerprint())
            report = reconcile_markdown(config, verify=audit)
            result["local"] = asdict(report)
            result["local_ready"] = not report.degraded
            # Make local progress visible before a possibly offline release check.
            atomic_write_json(root / "maintenance.json", result)
            if not report.degraded:
                from vaws_knowledge.publishing import run_once

                shared = run_once(config, force=force and verify, verify=audit)
                result["shared"] = shared
                sync = shared.get("sync") or {}
                result["ready"] = sync.get("status", shared.get("status")) in {"unchanged", "switched", "disabled"}
                # A newly downloaded release can pin different model files.
                # Local vectors must agree before preparation reports ready.
                if model_before != dict(backend.index_fingerprint()):
                    report = reconcile_markdown(config, verify=True)
                    result["local"] = asdict(report)
                    result["local_ready"] = not report.degraded
                    result["ready"] = result["ready"] and not report.degraded
                if audit and result["ready"]:
                    result["next_verify"] = now + VERIFY_SECONDS
        except Exception as exc:
            result["reason"] = f"{type(exc).__name__}: {exc}"[:1000]
        result["status"] = "ready" if result["ready"] else "pending"
        if not result["ready"]:
            result["next_check"] = now + 60
            result["next_verify"] = now + 60
        atomic_write_json(root / "maintenance.json", result)
        return result
    finally:
        lock.release()


class MaintenanceWorker:
    """One quiet thread per MCP connection, sharing the process lock above."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.closed = threading.Event()
        self.wakeup = threading.Event()
        self.thread: threading.Thread | None = None
        # Create the client holder before either thread uses it.
        backend_for_config(config)

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="knowledge-maintenance", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while not self.closed.is_set():
            changed = self.wakeup.is_set()
            self.wakeup.clear()
            try:
                # Connections share readiness and the verification schedule.
                # A new client alone does not invalidate a prepared model.
                maintain(self.config, force=changed)
            except Exception:
                pass  # Retry later; maintenance cannot terminate the MCP stream.
            self.wakeup.wait(POLL_SECONDS)

    def request(self) -> None:
        self.wakeup.set()

    def stop(self) -> None:
        self.closed.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=1)


def project_config(project: Path, path: Path | None = None) -> ServiceConfig:
    """Supply local defaults while preserving existing mounts and permissions."""
    project = project.expanduser().resolve()
    path = (path or project / ".vaws-local" / "knowledge" / "service.json").expanduser().resolve()
    payload = read_json(path) if path.exists() else {}
    if not isinstance(payload, dict):
        raise ValueError("existing knowledge configuration must be a JSON object")
    payload.setdefault("state_root", str(path.parent / "instance"))
    layers = payload.setdefault("layers", {})
    layers.setdefault("project", {"roots": [str(project / ".agents" / "knowledge")]})
    layers.setdefault("candidate", {"roots": [str(path.parent / "candidate")]})
    payload.setdefault("shared_sync", {"enabled": True})
    payload.setdefault("publishing", {"enabled": False})
    atomic_write_json(path, payload)
    return load_config(path=path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare local knowledge models, sources and verified indexes")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        config = project_config(args.project, args.config)
        result = maintain(config, verify=True, force=True)
        result["config"] = str(config.config_path)
    except Exception as exc:
        result = {"status": "pending", "ready": False, "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ready") else 1
