"""Retrieval backends.

Production uses OpenViking. ``MemoryBackend`` is a test double, not a second
product index. ``UnavailableBackend`` lets query/capture keep working when
the local instance is down: Markdown remains authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from vaws_knowledge.markdown import layer_from_uri

_TOKEN_RE = re.compile(r"[a-z0-9_.+/-]{2,}")


@dataclass(frozen=True)
class Hit:
    uri: str
    score: float
    title: str = ""
    excerpt: str = ""
    layer: str = ""
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "score": round(self.score, 4),
            "title": self.title,
            "excerpt": self.excerpt,
            "layer": self.layer,
        }


class RetrievalBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """Return ``(ok, detail)``. ``ok`` false means index is unusable."""

    def upsert(self, uri: str, content: str, *, layer: str, wait: bool = True) -> None:
        """Create or replace one document in the index."""

    def delete(self, uri: str) -> None:
        """Remove one document from the index. Missing URIs are not errors."""

    def search(
        self,
        text: str,
        *,
        layers: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        """Return ranked hits. Caller applies known-condition filtering."""

    def read(self, uri: str) -> str | None:
        """Return indexed content, or None if absent."""


class UnavailableBackend:
    """Index is down. Capture still writes Markdown; query reports degraded."""

    name = "unavailable"

    def __init__(self, reason: str = "knowledge index is unavailable"):
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def upsert(self, uri: str, content: str, *, layer: str, wait: bool = True) -> None:
        del uri, content, layer, wait

    def delete(self, uri: str) -> None:
        del uri

    def search(
        self,
        text: str,
        *,
        layers: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        del text, layers, limit
        return []

    def read(self, uri: str) -> str | None:
        del uri
        return None


class MemoryBackend:
    """In-process store for tests. Not used in production."""

    name = "memory"

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def available(self) -> tuple[bool, str]:
        return True, "memory"

    def upsert(self, uri: str, content: str, *, layer: str, wait: bool = True) -> None:
        del wait
        title = ""
        body = content
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        self.documents[uri] = {
            "uri": uri,
            "content": content,
            "layer": layer,
            "title": title,
            "body": body,
        }

    def delete(self, uri: str) -> None:
        self.documents.pop(uri, None)

    def read(self, uri: str) -> str | None:
        record = self.documents.get(uri)
        return None if record is None else str(record["content"])

    def search(
        self,
        text: str,
        *,
        layers: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        wanted = set(layers or ())
        query_tokens = set(_TOKEN_RE.findall((text or "").lower()))
        hits: list[Hit] = []
        for uri, record in self.documents.items():
            layer = str(record.get("layer") or layer_from_uri(uri) or "")
            if wanted and layer not in wanted:
                continue
            haystack = str(record.get("content") or "").lower()
            score = 0.0
            if text and text.lower() in haystack:
                score += 3.0
            if query_tokens:
                found = [tok for tok in query_tokens if tok in haystack]
                score += len(found)
            if score <= 0:
                continue
            content = str(record.get("content") or "")
            hits.append(
                Hit(
                    uri=uri,
                    score=score,
                    title=str(record.get("title") or ""),
                    excerpt=" ".join(content.split())[:240],
                    layer=layer,
                    content=content,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.uri))
        return hits[: max(int(limit or 8), 1)]


def backend_for_config(config: Any) -> RetrievalBackend:
    """Select the retrieval backend from config/env. Never invent a second product index."""

    existing = getattr(config, "retrieval", None)
    if existing is not None:
        return existing
    name = str(getattr(config, "backend", None) or "openviking").strip().lower()
    if name in {"memory", "test"}:
        backend: RetrievalBackend = MemoryBackend()
    elif name in {"unavailable", "off", "none"}:
        backend = UnavailableBackend("knowledge index disabled")
    else:
        try:
            from vaws_knowledge.local.openviking import OpenVikingBackend

            backend = OpenVikingBackend(config)
        except Exception as exc:  # noqa: BLE001 - a missing engine is a degraded index
            backend = UnavailableBackend(
                f"openviking unavailable: {type(exc).__name__}: {exc}"
            )
    try:
        config.retrieval = backend
    except Exception:  # noqa: BLE001 - frozen/simple configs still work
        pass
    return backend
