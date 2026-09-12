"""Title + body Markdown for public contribution.

This is a join point with local capture, not a second document store.
Capture writes ordinary Markdown; contribution uses the same parser.
Contribution reads that shape and writes the same shape to the public copy.
Path + Git SHA identify published content. The content digest is only for
idempotency, de-duplication, and integrity — it must not be passed off as a
Git identity.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path
from dataclasses import dataclass

from vaws_knowledge.markdown import parse_markdown, render_markdown, validate_kind as require_kind
from vaws_knowledge.contribution.errors import DocumentRejected, IdentityError

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
DIGEST_PREFIX = "sha256:"
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)


def require_title_body(text: str) -> tuple[str, str]:
    title, body = parse_markdown(text)
    if not body:
        raise DocumentRejected(["content is required"])
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
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise IdentityError("expected a Git commit SHA")
    return value.lower()


def public_filename(title: str, kind: str = "knowledge") -> str:
    """Allocate an identity once; later revisions keep this filename."""

    kind = require_kind(kind)
    if kind == "knowledge":
        semantic = re.sub(r"[^\w-]+", "-", title.strip().lower()).strip("-_")[:60].rstrip("-_")
        if _WINDOWS_RESERVED.match(semantic):
            semantic += "-entry"
        return f"{semantic or 'knowledge'}.md"
    return f"{secrets.token_hex(6)}-{slugify(title)}.md"


def require_relative_path(value: str) -> str:
    """A literal repository-relative POSIX path, safe on Windows too."""

    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(char) < 32 or char in '<>:"|?*' for char in value)
            or any(part.casefold() in {"", ".", "..", ".git"} or part.endswith((".", " "))
                   or _WINDOWS_RESERVED.match(part) for part in value.split("/"))):
        raise IdentityError("expected a safe relative POSIX path")
    return value


def require_public_relpath(value: str, kind: str) -> str:
    require_relative_path(value)
    parts = value.split("/")
    if len(parts) < 2 or parts[0] != require_kind(kind) or not value.endswith(".md"):
        raise IdentityError("public path must be kind/relative-file.md and match the content kind")
    return value


def safe_file_path(root: Path, relative: str) -> Path:
    """Reject symlink components before reading or writing contribution files."""

    require_relative_path(relative)
    root = Path(root)
    target = root
    if root.is_symlink():
        raise IdentityError("contribution root must not be a symlink")
    for part in relative.split("/"):
        target = target / part
        if target.is_symlink():
            raise IdentityError("contribution path must not contain symlinks")
    if not target.resolve().is_relative_to(root.resolve()):
        raise IdentityError("contribution path escaped its root")
    return target


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
