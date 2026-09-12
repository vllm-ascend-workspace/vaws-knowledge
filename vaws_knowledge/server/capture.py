"""Write path: candidate Markdown only.

Capture requires a title and non-empty content. Known source, conditions, and
evidence are kept when supplied; unknown values are omitted. Shared and
project layers are refused. Indexing through OpenViking is best-effort: a
down index still leaves the Markdown file on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from vaws_knowledge.contribution.documents import require_public_relpath, safe_file_path
from vaws_knowledge.contribution.errors import IdentityError
from vaws_knowledge.local.backend import backend_for_config
from vaws_knowledge.local.reconcile import remember_document
from vaws_knowledge.markdown import (
    delete_document,
    document_slug,
    iter_markdown_files,
    load_document,
    relative_posix,
    save_document,
    uri_for,
    utc_now,
)
from vaws_knowledge.server.layers import WRITABLE_LAYERS, ServiceConfig, load_config


class CaptureRefused(Exception):
    """The requested write is not allowed (wrong layer, read-only mount)."""

    def __init__(self, message: str, *, layer: str | None = None):
        super().__init__(message)
        self.layer = layer


class CaptureRejected(Exception):
    """The document itself is not well formed enough to store."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def candidate_root(config: ServiceConfig, *, create: bool = True) -> Path:
    mount = config.mount("candidate")
    if not mount.roots:
        raise CaptureRefused("candidate layer is not configured", layer="candidate")
    if mount.read_only:
        raise CaptureRefused("candidate layer is read-only", layer="candidate")
    root = Path(mount.roots[0])
    other = config.for_kind("experience" if config.kind == "knowledge" else "knowledge")
    resolved = root.resolve()
    for other_mount in other.mounts.values():
        if any(resolved.is_relative_to(path.resolve()) or path.resolve().is_relative_to(resolved)
               for path in other_mount.roots):
            raise CaptureRefused("candidate directory overlaps another content store", layer="candidate")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _proposed_identity(
    root: Path, heading: str, *, kind: str, ref: str | None = None,
    public_relpath: str | None = None,
) -> tuple[str, str, Path, bool]:
    if ref is not None and public_relpath is not None:
        raise CaptureRejected(["use either a candidate ref or a public_relpath"])
    if ref is not None:
        matches = []
        for candidate in iter_markdown_files(root, kind=kind):
            document = load_document(candidate, layer="candidate", root=root, kind=kind)
            if ref in {document.uri, str(document.path), document.path.name, document.slug}:
                matches.append(document)
        if len(matches) != 1:
            raise CaptureRejected(["candidate ref must identify exactly one document in this store"])
        document = matches[0]
        return document.slug, document.uri, document.path, True
    if public_relpath is not None:
        try:
            relative = require_public_relpath(public_relpath, kind).split("/", 1)[1]
            path = safe_file_path(root, relative)
        except IdentityError as exc:
            raise CaptureRejected([str(exc)]) from exc
        return path.stem, uri_for("candidate", relative, kind=kind), path, path.is_file()
    matches = []
    for candidate in iter_markdown_files(root, kind=kind):
        existing = load_document(candidate, layer="candidate", root=root, kind=kind)
        if existing.title.strip() == heading:
            matches.append(existing)
    if len(matches) > 1:
        raise CaptureRejected(["multiple candidates share this title; specify ref or public_relpath"])
    if matches:
        existing = matches[0]
        return existing.slug, existing.uri, existing.path, True
    ident = document_slug(heading)
    path = root / f"{ident}.md"
    return ident, uri_for("candidate", relative_posix(path, root), kind=kind), path, False


def capture(
    *,
    title: str | None = None,
    content: str | None = None,
    ref: str | None = None,
    public_relpath: str | None = None,
    layer: str = "candidate",
    config: ServiceConfig | None = None,
    source: Mapping[str, Any] | None = None,
    conditions: Mapping[str, Any] | None = None,
    evidence: Any = None,
    dry_run: bool = False,
    index: bool = True,
) -> dict[str, Any]:
    """Save one candidate document. Required inputs are title and content."""

    if layer != "candidate":
        raise CaptureRefused(
            f"refusing to write into layer {layer!r}: capture only ever writes the "
            f"candidate layer. Public contribution is a separate step.",
            layer=layer,
        )
    if layer not in WRITABLE_LAYERS:
        raise CaptureRefused(f"layer {layer!r} is not writable", layer=layer)

    heading = (title or "").strip()
    body = (content or "").strip()
    problems: list[str] = []
    if not heading:
        problems.append("title is required")
    if not body:
        problems.append("content is required")
    if problems:
        raise CaptureRejected(problems)

    config = config or load_config()
    root = candidate_root(config, create=not dry_run)
    ident, uri, path, updating = _proposed_identity(
        root, heading, kind=config.kind, ref=ref, public_relpath=public_relpath,
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "title": heading,
            "slug": ident,
            "uri": uri,
            "path": str(path),
            "layer": "candidate",
            "kind": config.kind,
            "index": "skipped",
            "would_update": updating,
        }

    document = save_document(
        root,
        layer="candidate",
        kind=config.kind,
        title=heading,
        content=body,
        path=path if updating or public_relpath is not None else None,
        slug=None if updating else ident,
        source=source,
        conditions=conditions,
        evidence=evidence,
        captured_at=utc_now(),
    )

    backend = backend_for_config(config) if index else None
    ok, detail = backend.available() if backend is not None else (False, "indexing deferred to retrieval")
    indexed = False
    index_error = None
    if ok:
        try:
            backend.upsert(document.uri, document.path.read_text(encoding="utf-8"), layer="candidate")
            indexed = True
            remember_document(config, document)
        except Exception as exc:  # noqa: BLE001 - Markdown is already saved
            index_error = f"{type(exc).__name__}: {exc}"
    else:
        index_error = detail
    payload = {
        "ok": True,
        "title": document.title,
        "slug": document.slug,
        "uri": document.uri,
        "ref": document.uri,
        "path": str(document.path),
        "layer": "candidate",
        "kind": document.kind,
        "index": "ready" if indexed else "pending",
        "document": document.to_dict(),
    }
    if document.source:
        payload["source"] = dict(document.source)
    if document.conditions:
        payload["conditions"] = dict(document.conditions)
    if not indexed:
        payload["index_detail"] = index_error
        payload["degraded"] = bool(index)
    from vaws_knowledge.publishing import queue_capture

    payload["contribution"] = queue_capture(config, document.path, public_relpath=public_relpath)
    return payload


def delete(
    ref: str,
    *,
    config: ServiceConfig | None = None,
) -> dict[str, Any]:
    """Delete one candidate document and drop it from the index."""

    config = config or load_config()
    root = candidate_root(config)
    target = None
    for path in iter_markdown_files(root, kind=config.kind):
        document = load_document(path, layer="candidate", root=root, kind=config.kind)
        if ref in {document.uri, document.slug, str(document.path), document.path.name}:
            target = document
            break
    if target is None:
        raise CaptureRejected([f"candidate not found: {ref}"])
    backend = backend_for_config(config)
    ok, _detail = backend.available()
    if ok:
        try:
            backend.delete(target.uri)
        except Exception:  # noqa: BLE001 - still delete the file
            pass
    delete_document(target.path)
    return {"ok": True, "deleted": target.uri, "path": str(target.path), "kind": target.kind}
