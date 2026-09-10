"""Write path: candidate Markdown only.

Capture requires a title and non-empty content. Known source, conditions, and
evidence are kept when supplied; unknown values are omitted. Shared and
project layers are refused. Indexing through OpenViking is best-effort: a
down index still leaves the Markdown file on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from vaws_knowledge.canonical import canonical_json
from vaws_knowledge.canonical import content_hash as packaged_content_hash
from vaws_knowledge.local.backend import backend_for_config
from vaws_knowledge.local.reconcile import remember_document
from vaws_knowledge.markdown import (
    delete_document,
    document_slug,
    find_by_title,
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


def canonical_payload(entry: Mapping[str, Any]) -> str:
    """YAML-corpus helper kept for review/conformance; not used by Markdown capture."""

    return canonical_json(entry)


def builtin_content_hash(entry: Mapping[str, Any]) -> str:
    """YAML-corpus helper kept for review/conformance; not used by Markdown capture."""

    return packaged_content_hash(entry)


def candidate_root(config: ServiceConfig, *, create: bool = True) -> Path:
    mount = config.mount("candidate")
    if not mount.roots:
        raise CaptureRefused("candidate layer is not configured", layer="candidate")
    root = Path(mount.roots[0])
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _proposed_identity(root: Path, heading: str) -> tuple[str, str, Path, bool]:
    existing = find_by_title(root, heading, layer="candidate")
    if existing is not None:
        return existing.slug, existing.uri, existing.path, True
    ident = document_slug(heading)
    path = root / f"{ident}.md"
    return ident, uri_for("candidate", relative_posix(path, root)), path, False


def _title_and_content(
    *,
    title: str | None,
    content: str | None,
    entry: Mapping[str, Any] | None,
) -> tuple[str, str]:
    heading = (title or "").strip()
    body = (content or "").strip()
    if (not heading or not body) and isinstance(entry, Mapping):
        heading = heading or str(entry.get("title") or "").strip()
        body = body or str(entry.get("content") or "").strip()
    return heading, body


def capture(
    entry: Mapping[str, Any] | None = None,
    *,
    title: str | None = None,
    content: str | None = None,
    kind: str = "note",
    layer: str = "candidate",
    config: ServiceConfig | None = None,
    source: Mapping[str, Any] | None = None,
    conditions: Mapping[str, Any] | None = None,
    evidence: Any = None,
    dry_run: bool = False,
    **_ignored: Any,
) -> dict[str, Any]:
    """Save one candidate document. Required inputs are title and content."""

    del kind
    if layer != "candidate":
        known = "shared/project are review-gated; " if layer in ("shared", "project") else ""
        raise CaptureRefused(
            f"refusing to write into layer {layer!r}: capture only ever writes the "
            f"candidate layer. {known}public contribution is a later, separate step.",
            layer=layer,
        )
    if layer not in WRITABLE_LAYERS:
        raise CaptureRefused(f"layer {layer!r} is not writable", layer=layer)

    heading, body = _title_and_content(title=title, content=content, entry=entry)
    problems: list[str] = []
    if not heading:
        problems.append("title is required")
    if not body:
        problems.append("content is required")
    if problems:
        raise CaptureRejected(problems)

    extra_source = source
    extra_conditions = conditions
    extra_evidence = evidence
    if isinstance(entry, Mapping):
        extra_source = extra_source or (
            entry.get("source") if isinstance(entry.get("source"), Mapping) else None
        )
        extra_conditions = extra_conditions or (
            entry.get("conditions") if isinstance(entry.get("conditions"), Mapping) else None
        )
        if extra_evidence is None:
            extra_evidence = entry.get("evidence")

    config = config or load_config()
    root = candidate_root(config, create=not dry_run)
    ident, uri, path, updating = _proposed_identity(root, heading)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "title": heading,
            "slug": ident,
            "uri": uri,
            "path": str(path),
            "layer": "candidate",
            "index": "skipped",
            "would_update": updating,
        }

    document = save_document(
        root,
        layer="candidate",
        title=heading,
        content=body,
        path=path if updating else None,
        slug=None if updating else ident,
        status="unverified",
        source=extra_source,
        conditions=extra_conditions,
        evidence=extra_evidence,
        captured_at=utc_now(),
    )

    backend = backend_for_config(config)
    ok, detail = backend.available()
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
        "status": document.status,
        "index": "ready" if indexed else "pending",
        "document": document.to_dict(),
    }
    if extra_source:
        payload["source"] = dict(extra_source)
    if document.conditions:
        payload["conditions"] = dict(document.conditions)
    if not indexed:
        payload["index_detail"] = index_error
        payload["degraded"] = True
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
    for path in iter_markdown_files(root):
        document = load_document(path, layer="candidate", root=root)
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
    return {"ok": True, "deleted": target.uri, "path": str(target.path)}
