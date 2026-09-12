"""Capture-to-PR and Release-to-shared lifecycle inside the local MCP service.

Only newly captured candidates are queued. The existing pending store and
OVPack sync own their state. This worker neither reviews nor merges a PR.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from vaws_knowledge.contribution.pending import iter_pending, save_pending
from vaws_knowledge.contribution.submit import SubmitConfig, prepare_candidate, submit_pending
from vaws_knowledge.distribution.errors import SwitchInProgress
from vaws_knowledge.distribution.manifest import atomic_write_json, read_json
from vaws_knowledge.distribution.sync import SwitchLock, check_and_sync
from vaws_knowledge.local.instance import instance_for_config
from vaws_knowledge.server.layers import ServiceConfig, load_config

DEFAULT_CORPUS = "vllm-ascend-workspace/vaws-knowledge-corpus"


def queue_capture(config: ServiceConfig, candidate: Path) -> dict[str, Any]:
    settings = config.publishing
    if not settings.get("enabled") or not settings.get("fork"):
        return {"status": "local_only"}
    try:
        root = instance_for_config(config).state_root
        record = prepare_candidate(candidate, state_root=root, public_root=root / "contribution" / "public")
        return {"status": record.status, "pr_url": record.pr_url}
    except Exception as exc:  # local capture is already durable
        return {"status": "prepare_failed", "reason": str(exc)[:600]}


def run_once(config: ServiceConfig, *, force: bool = False, verify: bool = False) -> dict[str, Any]:
    settings = config.publishing
    shared = getattr(config, "shared_sync", {})
    # Empty roots represent an explicit disabled shared layer. Its sync must
    # not start a model/server or contact a release source either.
    shared_enabled = shared.get("enabled", True) and bool(config.mount("shared").roots)
    publishing_enabled = settings.get("enabled", False)
    if not shared_enabled and not publishing_enabled:
        return {"status": "disabled", "sync": {"status": "disabled"}}
    instance = instance_for_config(config)
    root = instance.state_root
    status_path = root / "publishing.json"
    lock = SwitchLock(root / "publishing.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        return {"status": "busy", "sync": {"status": "busy"}}
    try:
        previous = read_json(status_path) or {}
        now = time.time()
        if not force and not verify and now < previous.get("next_check", 0):
            return {"status": "unchanged", "sync": previous.get("sync", {"status": "pending"})}
        result: dict[str, Any] = {"status": "ok", "checked_at": now,
                                  "next_check": now + 30, "next_sync": previous.get("next_sync", 0)}
        if publishing_enabled and settings.get("fork"):
            pending = [r for r in iter_pending(root) if r.status in {"pending", "awaiting_transport", "pr_open"}]
            if pending:
                try:
                    from vaws_knowledge.github_transport import github_token
                    from vaws_knowledge.contribution.github import UrllibContributionGitHub

                    github = UrllibContributionGitHub(github_token())
                    submit_config = SubmitConfig(
                        upstream=settings["repository"], fork=settings["fork"],
                        default_branch=settings.get("default_branch", "main"), push_remote="origin",
                    )
                    result["contributions"] = []
                    submissions = [r for r in pending if r.status != "pr_open"][:10]
                    reviews = [r for r in pending if r.status == "pr_open"]
                    cursor = int(previous.get("review_cursor", 0)) % max(1, len(reviews))
                    batch = (reviews[cursor:] + reviews[:cursor])[:10]
                    result["review_cursor"] = cursor + len(batch)
                    for record in submissions + batch:
                        if record.status == "pr_open":
                            pull = github.get(f"/repos/{settings['repository']}/pulls/{record.pr_number}")
                            if pull.get("state") == "closed":
                                record.status = "merged" if pull.get("merged") else "closed"
                                save_pending(root, record)
                            result["contributions"].append({"status": record.status, "pr_url": record.pr_url})
                            continue
                        saved = submit_pending(
                            record, state_root=root, public_root=root / "contribution" / "public",
                            git_repo=Path(settings["git_repo"]), github=github, config=submit_config,
                        )
                        result["contributions"].append({"status": saved.status, "pr_url": saved.pr_url,
                                                         "reason": saved.last_error})
                except Exception as exc:
                    result["contribution_error"] = str(exc)[:1000]
                    result["status"] = "partial"
        if not shared_enabled:
            result["sync"] = {"status": "disabled"}
        elif force or verify or now >= result["next_sync"]:
            try:
                from vaws_knowledge.distribution.release import source_from_location
                from vaws_knowledge.distribution.client import connect_client
                from vaws_knowledge.local.embedding import EMBEDDING_MODEL, EMBEDDING_DIMENSION

                repository = shared.get("repository") or settings.get("repository") or DEFAULT_CORPUS
                source = source_from_location(f"github://{repository}", cache_dir=root / "release-downloads")
                status: dict[str, Any] = {}
                client = None

                def prepare_model(manifest):
                    status.update(instance.ensure(verify_model=True, model_manifest=manifest))

                def open_client():
                    nonlocal client
                    client = connect_client(status["openviking_url"], api_key=instance.data_key())
                    return client

                try:
                    synced = check_and_sync(
                        root, source, embedding_info={"model": EMBEDDING_MODEL, "dimension": EMBEDDING_DIMENSION},
                        client_factory=open_client, model_cache=instance.cache_dir, prepare_model=prepare_model,
                        verify=verify,
                    )
                finally:
                    if client is not None:
                        client.close()
                result["sync"] = synced.to_dict()
                result["next_sync"] = now + (1800 if synced.ok else 60)
            except Exception as exc:
                result["sync"] = {"status": "error", "reason": str(exc)[:1000]}
                result["next_sync"] = now + 60
        else:
            result["sync"] = previous.get("sync")
        if (result.get("sync") or {}).get("status") not in {"switched", "unchanged", "disabled"}:
            result["status"] = "partial"
        if any(item.get("status") == "awaiting_transport" for item in result.get("contributions", [])):
            result["status"] = "partial"
        atomic_write_json(status_path, result)
        return result
    finally:
        lock.release()


def configure(path: Path, *, repository: str, read_only: bool = False) -> dict[str, Any]:
    from vaws_knowledge.github_transport import ensure_fork, repository_name

    path = Path(path).expanduser().resolve()
    payload = read_json(path) if path.exists() else {}
    if not isinstance(payload, dict):
        raise ValueError("existing knowledge configuration must be a JSON object")
    payload.setdefault("state_root", str(path.parent / "instance"))
    config = load_config(payload, path=path, base_dir=path.parent)
    root = instance_for_config(config).state_root.resolve()
    name = repository_name(repository)
    payload["shared_sync"] = {"enabled": True, "repository": name}
    settings: dict[str, Any] = {"enabled": not read_only, "repository": name}
    if read_only and isinstance(payload.get("publishing"), dict):
        settings = {**payload["publishing"], **settings}
    if not read_only:
        settings.update(ensure_fork(repository, root / "contribution" / "repository"))
    payload["publishing"] = settings
    payload["state_root"] = str(root)
    atomic_write_json(path, payload)
    return {"status": "configured", "config": str(path), "repository": repository,
            "contributions": not read_only, "review": "manual"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("configure", help="reuse/create a corpus fork and enable background publishing")
    setup.add_argument("--config", required=True)
    setup.add_argument("--repository", default=DEFAULT_CORPUS)
    setup.add_argument("--read-only", action="store_true")
    for name in ("once", "status"):
        command = sub.add_parser(name)
        command.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            result = configure(Path(args.config), repository=args.repository, read_only=args.read_only)
        else:
            config = load_config(path=args.config)
            root = instance_for_config(config).state_root
            result = run_once(config, force=True) if args.command == "once" else {
                "enabled": bool(config.publishing.get("enabled")),
                "last_check": read_json(root / "publishing.json"),
                "contributions": [{"title": r.title, "status": r.status, "pr_url": r.pr_url,
                                    "reason": r.last_error} for r in iter_pending(root)],
            }
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
