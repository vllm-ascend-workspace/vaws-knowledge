"""Query and explain Markdown knowledge through OpenViking.

Retrieval is native. Known conditions may drop a hit that is *explicitly*
inapplicable; unknown conditions stay. An unavailable index is labelled
degraded and is never an authoritative "no".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from vaws_knowledge import package_version
from vaws_knowledge.local.backend import Hit, backend_for_config
from vaws_knowledge.markdown import Document, iter_markdown_files, layer_from_uri, load_document
from vaws_knowledge.server.layers import LAYERS, ServiceConfig, shared_source

NO_RESULT_MEANING = (
    "No document matched. That means UNKNOWN, not supported and not absent-therefore-fine. "
    "Treat a missing fact as unexamined. If the index is degraded, this is not a complete search."
)
REFERENCE_NOTE = (
    "All knowledge is reference, not an axiom. Local experience and public "
    "documents are returned together by relevance and known applicability. "
    "Public review status is not an admission or ranking filter and does not "
    "prove hardware facts."
)

#: Optional known-condition names. None are required.
CONDITION_KEYS: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "python_abi",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
    "model",
    "topology",
    "execution_mode",
    "component",
)

# Historical aliases kept so older callers compiling against 0.2.0 names
# still import. They are not a YAML applicability engine.
SCOPE_DIMENSIONS = CONDITION_KEYS
READER_DIMENSIONS = CONDITION_KEYS


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def known_conditions(raw: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        text = _norm(value)
        if not text or text.lower() == "unknown":
            continue
        out[str(key)] = text
    return out


def conditions_conflict(entry: Mapping[str, str], reader: Mapping[str, str]) -> list[str]:
    """Return keys where both sides have a concrete value and they differ."""

    mismatched: list[str] = []
    for key, reader_value in reader.items():
        entry_value = entry.get(key)
        if not entry_value or not reader_value:
            continue
        if entry_value.lower() != reader_value.lower() and entry_value != reader_value:
            mismatched.append(key)
    return mismatched


def load_layer_documents(config: ServiceConfig, layers: Sequence[str]) -> list[Document]:
    documents: list[Document] = []
    for layer in layers:
        mount = config.mount(layer)
        if not mount.present:
            continue
        for root in mount.roots:
            for path in iter_markdown_files(Path(root)):
                try:
                    documents.append(load_document(path, layer=layer))
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
    filtered_before_limit: int = 0
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
            "filtered_before_limit": self.filtered_before_limit,
            "notes": list(self.notes),
            "results": list(self.results),
        }
        if self.index_detail:
            payload["index_detail"] = self.index_detail
        payload.update(shared_source())
        return payload


def _hit_payload(hit: Hit, document: Document | None, *, mismatched: list[str]) -> dict[str, Any]:
    title = (document.title if document else None) or hit.title
    excerpt = (document.excerpt() if document else None) or hit.excerpt
    layer = (document.layer if document else None) or hit.layer or layer_from_uri(hit.uri) or ""
    payload: dict[str, Any] = {
        "ref": hit.uri,
        "uri": hit.uri,
        "title": title,
        "excerpt": excerpt,
        "layer": layer,
        "status": document.status if document else "unknown",
        "role": "reference",
        "score": round(hit.score, 4),
        "applies": not mismatched,
    }
    if document and document.source:
        payload["source"] = dict(document.source)
    if document and document.conditions:
        payload["conditions"] = dict(document.conditions)
    if mismatched:
        payload["mismatched_conditions"] = mismatched
    if document:
        payload["path"] = str(document.path)
        payload["slug"] = document.slug
    return payload


def query(
    config: ServiceConfig,
    *,
    text: str | None = None,
    fingerprint: str | None = None,
    reader_coordinate: Mapping[str, Any] | None = None,
    conditions: Mapping[str, Any] | None = None,
    layers: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    include_unverified: bool = True,
    include_non_matching: bool = False,
    kind: str | None = None,
    bodies: Sequence[str] | None = None,
    limit: int = 8,
    today: Any = None,
    load: Any = None,
) -> QueryResponse:
    """Search mounted Markdown layers.

    Shared, project, and candidate are queried together. Review status is a
    label, not a filter or rank. Known reader conditions exclude only explicit
    mismatches after retrieval, so unknown items are not dropped by a short
    pre-filter.
    """

    del fingerprint, kind, bodies, today, load, statuses, include_unverified
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYERS]
    consulted = config.consulted(wanted_layers)
    reader = known_conditions(conditions if conditions is not None else reader_coordinate)
    request = {
        "text": text or "",
        "layers": wanted_layers,
        "conditions": reader,
        "limit": int(limit or 8),
    }
    backend = backend_for_config(config)
    ok, detail = backend.available()
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

    fetch = max(int(limit or 8) * 4, 16)
    hits = backend.search(text or "", layers=wanted_layers, limit=fetch)
    catalog = documents_by_uri(config, wanted_layers)
    kept: list[dict[str, Any]] = []
    filtered = 0
    for hit in hits:
        document = catalog.get(hit.uri)
        mismatched = conditions_conflict(document.conditions if document else {}, reader)
        if mismatched and not include_non_matching:
            filtered += 1
            continue
        kept.append(_hit_payload(hit, document, mismatched=mismatched))
    cap = max(int(limit or 8), 1)
    kept.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("uri") or "")))
    return QueryResponse(
        results=kept[:cap],
        degraded=consulted["degraded"],
        unavailable=False,
        index_detail=detail,
        notes=notes,
        request=request,
        layers_available=consulted["layers_available"],
        layers_absent=consulted["layers_absent"],
        filtered_before_limit=filtered,
        inspected=len(hits),
    )


def explain(
    config: ServiceConfig,
    ref: str,
    *,
    reader_coordinate: Mapping[str, Any] | None = None,
    layers: Sequence[str] | None = None,
    today: Any = None,
    load: Any = None,
    uuid: str | None = None,
) -> dict[str, Any]:
    """Expand one document by URI, slug, or path. Markdown on disk is authority."""

    del today, load
    ident = (ref or uuid or "").strip()
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
    if match is None:
        base.update(
            found=False,
            meaning=(
                "No document with this ref in the consulted layers. That is 'unknown': "
                "the document may exist in a layer that is not mounted."
            ),
        )
        return base
    reader = known_conditions(reader_coordinate)
    mismatched = conditions_conflict(match.conditions, reader)
    payload = match.to_dict()
    payload.update(found=True, applies=not mismatched, role="reference")
    payload.setdefault("notes", []).append(REFERENCE_NOTE)
    if mismatched:
        payload["mismatched_conditions"] = mismatched
    payload.update(base)
    payload["found"] = True
    return payload


def add_reader_coordinate_arguments(parser: Any) -> None:
    """Optional known-condition flags. Omitted dimensions stay unknown."""

    for name in CONDITION_KEYS:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, default=None)


def reader_coordinate_from_args(args: Any) -> dict[str, str]:
    raw = {name: getattr(args, name, None) for name in CONDITION_KEYS}
    return known_conditions(raw)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Query local Markdown knowledge")
    parser.add_argument("--text", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--backend", default="")
    add_reader_coordinate_arguments(parser)
    args = parser.parse_args(argv)
    from vaws_knowledge.server.layers import load_config

    mapping = {"backend": args.backend} if args.backend else None
    config = load_config(mapping, path=args.config or None)
    if args.ref:
        payload = explain(config, args.ref, reader_coordinate=reader_coordinate_from_args(args))
    else:
        if not str(args.text or "").strip():
            print("error: --text is required unless --ref is set", file=sys.stderr)
            return 2
        payload = query(
            config,
            text=args.text,
            conditions=reader_coordinate_from_args(args),
            limit=args.limit,
        ).to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
