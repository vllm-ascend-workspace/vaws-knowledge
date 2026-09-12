"""Query and explain Markdown knowledge through OpenViking.

Retrieval returns references, never applicability decisions. Recorded conditions
remain visible for the reader to assess. An unavailable index is labelled
degraded and does not block independent work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from vaws_knowledge import package_version
from vaws_knowledge.local.backend import Hit, backend_for_config
from vaws_knowledge.local.instance import instance_for_config
from vaws_knowledge.local.shared import current_shared
from vaws_knowledge.markdown import Document, iter_markdown_files, layer_from_uri, load_document, parse_markdown
from vaws_knowledge.server.layers import LAYERS, ServiceConfig, shared_source

NO_RESULT_MEANING = (
    "No document matched. That means UNKNOWN, not supported and not absent-therefore-fine. "
    "Treat a missing fact as unexamined. If the index is degraded, this is not a complete search."
)
REFERENCE_NOTE = (
    "All knowledge is reference, not an axiom. Local experience and public "
    "documents are returned together by relevance. "
    "Public review status is not an admission or ranking filter and does not "
    "prove hardware facts."
)

def load_layer_documents(config: ServiceConfig, layers: Sequence[str]) -> list[Document]:
    documents: list[Document] = []
    for layer in layers:
        mount = config.mount(layer)
        if not mount.present:
            continue
        for root in mount.roots:
            base = Path(root)
            for path in iter_markdown_files(base):
                try:
                    documents.append(load_document(path, layer=layer, root=base))
                except (OSError, UnicodeDecodeError):
                    continue
    return documents


def documents_by_uri(config: ServiceConfig, layers: Sequence[str]) -> dict[str, Document]:
    return {document.uri: document for document in load_layer_documents(config, layers)}


@dataclass
class QueryResponse:
    results: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    unavailable: bool = False
    index_detail: str = ""
    notes: list[str] = field(default_factory=list)
    request: dict[str, Any] = field(default_factory=dict)
    layers_available: list[str] = field(default_factory=list)
    layers_absent: dict[str, str] = field(default_factory=dict)
    inspected: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "version": package_version(),
            "request": self.request,
            "layers_available": list(self.layers_available),
            "layers_absent": dict(self.layers_absent),
            "degraded": self.degraded or self.unavailable,
            "unavailable": self.unavailable,
            "absent_fact_semantics": "unknown",
            "no_result_meaning": NO_RESULT_MEANING,
            "count": len(self.results),
            "inspected": self.inspected,
            "notes": list(self.notes),
            "results": list(self.results),
        }
        if self.index_detail:
            payload["index_detail"] = self.index_detail
        payload.update(shared_source())
        return payload


def _hit_payload(hit: Hit, document: Document | None) -> dict[str, Any]:
    title = (document.title if document else None) or hit.title
    excerpt = (document.excerpt() if document else None) or hit.excerpt
    layer = (document.layer if document else None) or hit.layer or layer_from_uri(hit.uri) or ""
    payload: dict[str, Any] = {
        "ref": hit.uri,
        "uri": hit.uri,
        "title": title,
        "excerpt": excerpt,
        "layer": layer,
        "role": "reference",
        "score": round(hit.score, 4),
    }
    if document and document.status:
        payload["status"] = document.status
    if document and document.source:
        payload["source"] = dict(document.source)
    if document and document.conditions:
        payload["conditions"] = dict(document.conditions)
    if document:
        payload["path"] = str(document.path)
        payload["slug"] = document.slug
    return payload


def query(
    config: ServiceConfig,
    *,
    text: str,
    layers: Sequence[str] | None = None,
    limit: int = 8,
) -> QueryResponse:
    """Search all mounted Markdown by relevance, retaining recorded context."""

    if not text.strip():
        raise ValueError("text is required")
    if limit < 1:
        raise ValueError("limit must be positive")
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYERS]
    consulted = config.consulted(wanted_layers)
    request = {
        "text": text or "",
        "layers": wanted_layers,
        "limit": int(limit or 8),
    }
    backend = backend_for_config(config)
    ok, detail = backend.ready()
    notes: list[str] = [REFERENCE_NOTE]
    if not ok:
        notes.append(
            "knowledge index is unavailable; this is not evidence that no document exists"
        )
        return QueryResponse(
            results=[],
            degraded=True,
            unavailable=True,
            index_detail=detail,
            notes=notes,
            request=request,
            layers_available=consulted["layers_available"],
            layers_absent=consulted["layers_absent"],
        )

    from vaws_knowledge.maintenance import maintenance_status

    maintenance = maintenance_status(config)
    pending = not maintenance.get("ready", False)
    if pending:
        notes.append("Index maintenance is pending; results may be incomplete. The service retries in the background.")

    fetch = max(int(limit or 8) * 4, 16)
    searched_layers = consulted["layers_available"]
    try:
        hits = backend.search(text, layers=searched_layers, limit=fetch) if searched_layers else []
    except Exception as exc:
        return QueryResponse(degraded=True, unavailable=True,
                             index_detail=f"{type(exc).__name__}: {exc}", notes=notes, request=request,
                             layers_available=consulted["layers_available"], layers_absent=consulted["layers_absent"])
    catalog = documents_by_uri(config, wanted_layers)
    active = current_shared(instance_for_config(config).state_root) if "shared" in searched_layers else None
    active_prefix = str(active["root_uri"]).rstrip("/") + "/" if active else ""
    kept: list[dict[str, Any]] = []
    for hit in hits:
        document = catalog.get(hit.uri)
        # A lost ledger must not make deleted local files reappear as references.
        # Only the current imported pack has its authoritative source off disk.
        if document is None and not (active_prefix and hit.uri.startswith(active_prefix)):
            continue
        kept.append(_hit_payload(hit, document))
    cap = max(int(limit or 8), 1)
    kept.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("uri") or "")))
    return QueryResponse(
        results=kept[:cap],
        degraded=consulted["degraded"] or pending,
        unavailable=False,
        index_detail=detail,
        notes=notes,
        request=request,
        layers_available=consulted["layers_available"],
        layers_absent=consulted["layers_absent"],
        inspected=len(hits),
    )


def explain(
    config: ServiceConfig,
    ref: str,
    *,
    layers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read one document by URI, slug, or path, including its recorded context."""

    ident = ref.strip()
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYERS]
    consulted = config.consulted(wanted_layers)
    base: dict[str, Any] = {
        "version": package_version(),
        "ref": ident,
        "layers_consulted": consulted["layers_available"],
        "layers_absent": consulted["layers_absent"],
        "degraded": consulted["degraded"],
        "absent_fact_semantics": "unknown",
    }
    base.update(shared_source())
    if not ident:
        base.update(found=False, meaning="ref is required")
        return base
    documents = load_layer_documents(config, wanted_layers)
    match: Document | None = None
    for document in documents:
        if ident in {
            document.uri,
            document.slug,
            str(document.path),
            document.path.name,
            document.path.stem,
        }:
            match = document
            break
    if match is None and "shared" in consulted["layers_available"] and layer_from_uri(ident) == "shared":
        active = current_shared(instance_for_config(config).state_root)
        prefix = str(active["root_uri"]).rstrip("/") + "/" if active else ""
        if prefix and ident.startswith(prefix) and ".." not in ident.split("/"):
            backend = backend_for_config(config)
            try:
                ok, detail = backend.ready()
                if not ok:
                    return {**base, "found": False, "unavailable": True, "degraded": True, "index_detail": detail}
                raw = backend.read(ident)
            except Exception as exc:
                return {**base, "found": False, "unavailable": True, "degraded": True,
                        "index_detail": f"{type(exc).__name__}: {exc}"}
            if raw:
                title, content = parse_markdown(raw)
                return {**base, "found": True, "title": title, "content": content,
                        "uri": ident, "layer": "shared", "role": "reference",
                        "source_git_sha": active.get("source_git_sha"), "notes": [REFERENCE_NOTE]}
    if match is None:
        base.update(
            found=False,
            meaning=(
                "No document with this ref in the consulted layers. That is 'unknown': "
                "the document may exist in a layer that is not mounted."
            ),
        )
        return base
    payload = match.to_dict()
    payload.update(found=True, role="reference")
    payload.setdefault("notes", []).append(REFERENCE_NOTE)
    payload.update(base)
    payload["found"] = True
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Query local Markdown knowledge")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--text", default="")
    selection.add_argument("--ref", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--backend", default="")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    from vaws_knowledge.server.layers import load_config

    mapping = {"backend": args.backend} if args.backend else None
    config = load_config(mapping, path=args.config or None)
    if args.ref:
        payload = explain(config, args.ref)
    else:
        if not str(args.text or "").strip():
            print("error: --text is required unless --ref is set", file=sys.stderr)
            return 2
        payload = query(
            config,
            text=args.text,
            limit=args.limit,
        ).to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
