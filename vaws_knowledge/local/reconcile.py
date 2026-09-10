"""Bring the live index in line with mounted project/candidate Markdown.

Markdown on disk is authority. Shared content is an imported OVPack version
owned by distribution; this module never walks or re-embeds it. Search still
uses ``current_shared(state_root)`` as the join point.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from vaws_knowledge.local.backend import backend_for_config
from vaws_knowledge.markdown import Document, iter_markdown_files, load_document

INDEX_LAYERS = ("project", "candidate")
STATE_NAME = "markdown-index.json"


@dataclass
class ReconcileReport:
    ok: bool = True
    upserted: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return not self.ok or bool(self.errors)


def _state_path(config: Any) -> Path | None:
    root = getattr(config, "state_root", None)
    if root is None:
        return None
    return Path(root) / STATE_NAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_state(config: Any) -> dict[str, Any]:
    path = _state_path(config)
    if path is None or not path.is_file():
        return {"documents": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"documents": {}}
    if not isinstance(loaded, dict):
        return {"documents": {}}
    documents = loaded.get("documents")
    if not isinstance(documents, dict):
        loaded["documents"] = {}
    return loaded


def _save_state(config: Any, state: dict[str, Any]) -> None:
    path = _state_path(config)
    if path is None:
        return
    try:
        _atomic_write_json(path, state)
    except OSError:
        pass


def file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under_roots(path: Path, roots: Sequence[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _mount_roots(config: Any, layers: Sequence[str]) -> list[Path]:
    roots: list[Path] = []
    for layer in layers:
        mount = config.mount(layer)
        if not mount.present:
            continue
        roots.extend(Path(item) for item in mount.roots)
    return roots


def _scan_documents(config: Any, layers: Sequence[str]) -> dict[str, Document]:
    found: dict[str, Document] = {}
    for layer in layers:
        if layer not in INDEX_LAYERS:
            continue
        mount = config.mount(layer)
        if not mount.present:
            continue
        for root in mount.roots:
            base = Path(root)
            for path in iter_markdown_files(base):
                try:
                    document = load_document(path, layer=layer, root=base)
                except (OSError, UnicodeDecodeError):
                    continue
                found[document.uri] = document
    return found


def remember_document(config: Any, document: Document) -> None:
    """Record a just-indexed document so the next query does not re-embed it."""

    try:
        fingerprint = file_fingerprint(document.path)
    except OSError:
        return
    state = _load_state(config)
    documents = state.setdefault("documents", {})
    documents[document.uri] = {
        "path": str(document.path.resolve()),
        "layer": document.layer,
        "sha256": fingerprint,
    }
    _save_state(config, state)


def reconcile_markdown(config: Any, layers: Sequence[str] | None = None) -> ReconcileReport:
    """Upsert new/changed local Markdown and drop index rows whose files are gone.

    Shared is never swept. Failures leave Markdown in place and are reported so
    the caller can mark the search degraded.
    """

    wanted = [name for name in (layers or INDEX_LAYERS) if name in INDEX_LAYERS]
    report = ReconcileReport()
    if not wanted:
        return report
    backend = backend_for_config(config)
    ok, detail = backend.available()
    if not ok:
        report.ok = False
        report.errors.append(detail)
        return report

    current = _scan_documents(config, wanted)
    state = _load_state(config)
    recorded: dict[str, Any] = state.setdefault("documents", {})
    roots = _mount_roots(config, wanted)

    for uri, document in current.items():
        try:
            fingerprint = file_fingerprint(document.path)
            content = document.path.read_text(encoding="utf-8")
        except OSError as exc:
            report.ok = False
            report.errors.append(f"{document.path}: {exc}")
            continue
        previous = recorded.get(uri) if isinstance(recorded.get(uri), dict) else None
        resolved = str(document.path.resolve())
        if (
            previous
            and previous.get("sha256") == fingerprint
            and previous.get("path") == resolved
        ):
            report.unchanged += 1
            continue
        try:
            backend.upsert(uri, content, layer=document.layer)
        except Exception as exc:  # noqa: BLE001 - Markdown stays; search is incomplete
            report.ok = False
            report.errors.append(f"{uri}: {type(exc).__name__}: {exc}")
            continue
        recorded[uri] = {
            "path": resolved,
            "layer": document.layer,
            "sha256": fingerprint,
        }
        report.upserted += 1

    for uri in list(recorded):
        record = recorded.get(uri)
        if not isinstance(record, dict):
            recorded.pop(uri, None)
            continue
        layer = str(record.get("layer") or "")
        if layer not in wanted:
            continue
        if uri in current:
            continue
        path = Path(str(record.get("path") or ""))
        if path.parts and not _under_roots(path, roots):
            continue
        try:
            backend.delete(uri)
        except Exception as exc:  # noqa: BLE001
            report.ok = False
            report.errors.append(f"delete {uri}: {type(exc).__name__}: {exc}")
            continue
        recorded.pop(uri, None)
        report.deleted += 1

    _save_state(config, state)
    return report
