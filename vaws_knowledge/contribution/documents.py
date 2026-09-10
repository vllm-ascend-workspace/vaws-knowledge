"""Title + body Markdown for public contribution.

This is a join point with local capture, not a second document store.
Capture (Grok 1) writes ordinary Markdown whose first heading is the title.
Contribution reads that shape and writes the same shape to the public copy.
Path + Git SHA identify published content. The content digest is only for
idempotency, de-duplication, and integrity — it must not be passed off as a
Git identity.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from vaws_knowledge.bot.publish_comment import _sha
from vaws_knowledge.contribution.errors import DocumentRejected, IdentityError

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
DIGEST_PREFIX = "sha256:"


def parse_title_body(text: str) -> tuple[str, str]:
    """Return ``(title, body)``. Title is the first ATX heading, else the first line."""

    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return "", ""
    first = lines[index].strip()
    if first.startswith("# "):
        title = first[2:].strip()
        body = "\n".join(lines[index + 1 :]).strip()
        return title, body
    return first, "\n".join(lines[index + 1 :]).strip()


def render_markdown(title: str, body: str) -> str:
    heading = (title or "").strip()
    content = (body or "").strip()
    if not heading:
        raise DocumentRejected(["title is required"])
    if not content:
        raise DocumentRejected(["content is required"])
    return f"# {heading}\n\n{content}\n"


def require_title_body(text: str) -> tuple[str, str]:
    title, body = parse_title_body(text)
    problems: list[str] = []
    if not title:
        problems.append("title is required")
    if not body:
        problems.append("content is required")
    if problems:
        raise DocumentRejected(problems)
    return title, body


def slugify(text: str) -> str:
    slug = _SLUG_UNSAFE.sub("-", (text or "").strip().lower()).strip("-")
    return slug[:60] or "contribution"


def canonical_bytes(title: str, body: str) -> bytes:
    return render_markdown(title, body).encode("utf-8")


def content_digest(title: str, body: str) -> str:
    digest = hashlib.sha256(canonical_bytes(title, body)).hexdigest()
    return DIGEST_PREFIX + digest


def digest_token(digest: str) -> str:
    text = str(digest or "")
    if text.startswith(DIGEST_PREFIX):
        text = text[len(DIGEST_PREFIX) :]
    if len(text) != 64:
        raise IdentityError("malformed content digest")
    try:
        int(text, 16)
    except ValueError as exc:
        raise IdentityError("malformed content digest") from exc
    return text.lower()


def require_git_sha(value: object) -> str:
    sha = _sha(value)
    if sha is None:
        raise IdentityError("content digest cannot stand in for git identity")
    return sha


def branch_for_digest(digest: str) -> str:
    token = digest_token(digest)
    return f"contrib/{token[:12]}"


def public_filename(digest: str, title: str) -> str:
    token = digest_token(digest)
    return f"{token[:12]}-{slugify(title)}.md"


@dataclass(frozen=True)
class ContentIdentity:
    """Published-content identity: a path at a Git object."""

    path: str
    git_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "git_sha", require_git_sha(self.git_sha))
        path = str(self.path or "").replace("\\", "/").strip().lstrip("/")
        if not path or ".." in path.split("/") or path.startswith(DIGEST_PREFIX):
            raise IdentityError("invalid content path")
        object.__setattr__(self, "path", path)

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "git_sha": self.git_sha}

    def label(self) -> str:
        return f"{self.path}@{self.git_sha}"


@dataclass(frozen=True)
class MarkdownDocument:
    title: str
    body: str
    digest: str

    @classmethod
    def from_text(cls, text: str) -> "MarkdownDocument":
        title, body = require_title_body(text)
        return cls(title=title, body=body, digest=content_digest(title, body))

    def render(self) -> str:
        return render_markdown(self.title, self.body)
