"""OpenViking native recall of related already-published documents.

The comparison set is the reviewed public library because that is what a
contribution must not duplicate, not because local content is less trusted.
Hits are not ranked by review status. Retrieval is by relevance; known
conditions travel with the document text when present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from vaws_knowledge.contribution.documents import (
    ContentIdentity,
    MarkdownDocument,
    parse_title_body,
    require_git_sha,
)
from vaws_knowledge.contribution.errors import IdentityError

DEFAULT_TARGET_URI = "viking://resources/shared"


@dataclass(frozen=True)
class RelatedDocument:
    identity: ContentIdentity
    title: str
    body: str
    uri: str = ""
    score: float = 0.0

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            **self.identity.as_dict(),
            "title": self.title,
            "uri": self.uri,
            "score": self.score,
        }
        return payload

    def document(self) -> MarkdownDocument:
        return MarkdownDocument.from_text(f"# {self.title}\n\n{self.body}\n")


@dataclass
class RecallResult:
    ok: bool
    documents: list[RelatedDocument] = field(default_factory=list)
    reason: str = ""
    target_uri: str = DEFAULT_TARGET_URI

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "target_uri": self.target_uri,
            "documents": [item.as_dict() for item in self.documents],
        }


class Recall(Protocol):
    def related(self, query: str, *, limit: int = 8) -> RecallResult: ...


class FixtureRecall:
    """In-memory recall for tests and offline fixtures."""

    def __init__(
        self,
        documents: Sequence[RelatedDocument] | None = None,
        *,
        fail: str | None = None,
        target_uri: str = DEFAULT_TARGET_URI,
    ) -> None:
        self.documents = list(documents or ())
        self.fail = fail
        self.target_uri = target_uri
        self.calls: list[str] = []

    def related(self, query: str, *, limit: int = 8) -> RecallResult:
        self.calls.append(query)
        if self.fail:
            return RecallResult(ok=False, reason=self.fail, target_uri=self.target_uri)
        hits = list(self.documents[: max(int(limit or 8), 1)])
        return RecallResult(ok=True, documents=hits, target_uri=self.target_uri)


class OpenVikingRecall:
    """Adapter over a native OpenViking client (``find``).

    ``client`` is injected. Production wiring uses ``openviking_sdk.SyncHTTPClient``
    or, once published, Grok 1's ``OpenVikingBackend`` via the join point below.
    """

    def __init__(
        self,
        client: Any,
        *,
        corpus_git_sha: str,
        target_uri: str = DEFAULT_TARGET_URI,
    ) -> None:
        self.client = client
        self.corpus_git_sha = require_git_sha(corpus_git_sha)
        self.target_uri = target_uri

    def related(self, query: str, *, limit: int = 8) -> RecallResult:
        try:
            payload = self.client.find(
                query or "",
                target_uri=self.target_uri,
                limit=max(int(limit or 8), 1),
                options={
                    "level": 2,
                    "read_content": True,
                    "score_threshold": 0,
                },
            )
        except Exception as exc:  # noqa: BLE001 — native client failures are data
            return RecallResult(
                ok=False,
                reason=f"OpenViking recall failed: {type(exc).__name__}: {exc}",
                target_uri=self.target_uri,
            )
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if not isinstance(resources, list):
            return RecallResult(
                ok=False,
                reason="OpenViking recall returned a malformed payload",
                target_uri=self.target_uri,
            )
        documents: list[RelatedDocument] = []
        for item in resources:
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri") or "")
            content = str(item.get("content") or item.get("text") or "")
            if not uri and not content:
                continue
            title, body = parse_title_body(content) if content else ("", "")
            path = _path_from_uri(uri) or (title or "document")
            blob = item.get("oid") or item.get("sha")
            try:
                git_sha = require_git_sha(blob) if blob else self.corpus_git_sha
            except IdentityError:
                git_sha = self.corpus_git_sha
            try:
                identity = ContentIdentity(path=path, git_sha=git_sha)
            except IdentityError:
                continue
            documents.append(
                RelatedDocument(
                    identity=identity,
                    title=title or path,
                    body=body or content,
                    uri=uri,
                    score=float(item.get("score") or item.get("similarity") or 0.0),
                )
            )
        documents.sort(key=lambda item: (-item.score, item.identity.path))
        return RecallResult(
            ok=True,
            documents=documents[: max(int(limit or 8), 1)],
            target_uri=self.target_uri,
        )


def _path_from_uri(uri: str) -> str:
    text = str(uri or "")
    marker = "viking://resources/"
    if text.startswith(marker):
        return text[len(marker) :]
    if "://" in text:
        return text.split("://", 1)[-1]
    return text.lstrip("/")
