"""Minimal Markdown knowledge documents.

A document is a title plus non-empty body. Optional known fields (source,
conditions, evidence, status) live in a sibling ``.meta.json`` so the Markdown
file stays ordinary prose. Unknown values are omitted, never invented.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

LAYERS = ("shared", "project", "candidate")
KINDS = ("knowledge", "experience")
URI_ROOT = "viking://resources"
SHARED_BOOTSTRAP_URI = f"{URI_ROOT}/shared/bootstrap"
_TITLE_DIGEST_LEN = 12


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def slugify(text: str) -> str:
    """ASCII filename helper. Not a unique document identity."""

    slug = _SLUG_UNSAFE.sub("-", (text or "").strip().lower()).strip("-")
    return slug[:80] or "captured-entry"


def title_digest(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:_TITLE_DIGEST_LEN]


def document_slug(title: str) -> str:
    """Stable, collision-resistant identity derived only from the title."""

    heading = (title or "").strip()
    digest = title_digest(heading)
    readable = slugify(heading)
    if readable == "captured-entry":
        return f"entry-{digest}"
    return f"{readable}-{digest}"


def relative_posix(path: Path, root: Path | None = None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            try:
                return Path(os.path.relpath(path, root)).as_posix()
            except ValueError:
                pass
    return Path(path).name


def _in_store(path: Path, root: Path, kind: str) -> bool:
    """Follow aliases before deciding whether a file belongs to this store."""

    opposite = set(KINDS) - {kind}
    try:
        lexical = path.relative_to(root)
        resolved = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return False
    return not opposite.intersection(lexical.parts[:-1] + resolved.parts[:-1])


def validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown content kind: {kind!r}")
    return kind


def uri_for(layer: str, relative: str, *, kind: str = "knowledge") -> str:
    validate_kind(kind)
    if layer not in LAYERS:
        raise ValueError(f"unknown layer: {layer!r}")
    name = str(relative or "").replace("\\", "/").lstrip("/")
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("document identity must be a relative file path")
    if not name.endswith(".md"):
        name = f"{name}.md"
    root = SHARED_BOOTSTRAP_URI if layer == "shared" else f"{URI_ROOT}/{layer}"
    return f"{root}/{kind}/{name}"


def layer_from_uri(uri: str) -> str | None:
    text = str(uri or "")
    prefix = URI_ROOT + "/"
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :]
    layer = rest.split("/", 1)[0]
    return layer if layer in LAYERS else None


def kind_from_uri(uri: str) -> str:
    """Read an explicit namespace; old untyped identities are not current facts."""

    prefix = URI_ROOT + "/"
    if not str(uri).startswith(prefix):
        return ""
    parts = str(uri)[len(prefix):].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    if len(parts) < 3 or parts[0] not in LAYERS:
        return ""
    index = 1
    if parts[0] == "shared":
        index = 4 if parts[1] == "repairs" else 2
        if parts[1] == "repairs" and (
            len(parts) < 6
            or not re.fullmatch(r"[0-9a-f]{16}", parts[2])
            or not re.fullmatch(r"v[0-9a-f]{12}", parts[3])
        ):
            return ""
    return parts[index] if len(parts) > index + 1 and parts[index] in KINDS else ""


def parse_markdown(text: str) -> tuple[str, str]:
    """Return ``(title, body)``. Title is the first ATX heading, else the first line."""

    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    match = _TITLE_RE.search(raw)
    if match:
        title = match.group(1).strip()
        body = (raw[: match.start()] + raw[match.end() :]).strip()
        return title, body
    body = raw.strip()
    title = next((line.strip() for line in raw.splitlines() if line.strip()), "untitled")
    return title, body


def render_markdown(title: str, content: str) -> str:
    heading = (title or "").strip() or "untitled"
    body = (content or "").strip()
    return f"# {heading}\n\n{body}\n" if body else f"# {heading}\n"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _clean_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not name:
            continue
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
            if not text or text.lower() == "unknown":
                continue
            out[name] = text
            continue
        out[name] = item
    return out or None


@dataclass
class Document:
    layer: str
    title: str
    content: str
    slug: str
    path: Path
    uri: str
    kind: str = "knowledge"
    status: str | None = None
    source: dict[str, Any] | None = None
    conditions: dict[str, str] = field(default_factory=dict)
    evidence: Any = None
    captured_at: str | None = None

    def excerpt(self, limit: int = 240) -> str:
        text = " ".join((self.content or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "layer": self.layer,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "slug": self.slug,
            "path": str(self.path),
            "uri": self.uri,
            "ref": self.uri,
        }
        if self.status:
            payload["status"] = self.status
        if self.source:
            payload["source"] = dict(self.source)
        if self.conditions:
            payload["conditions"] = dict(self.conditions)
        if self.evidence is not None:
            payload["evidence"] = self.evidence
        if self.captured_at:
            payload["captured_at"] = self.captured_at
        return payload


def meta_path(markdown_path: Path) -> Path:
    return markdown_path.with_suffix(".meta.json")


def load_document(
    path: Path, *, layer: str, root: Path | None = None, kind: str = "knowledge",
) -> Document:
    validate_kind(kind)
    text = path.read_text(encoding="utf-8")
    title, content = parse_markdown(text)
    rel = relative_posix(path, root)
    rel_slug = rel[:-3] if rel.lower().endswith(".md") else rel
    meta: dict[str, Any] = {}
    sidecar = meta_path(path)
    if sidecar.is_file():
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, Mapping):
            meta = dict(loaded)
    conditions: dict[str, str] = {}
    raw_conditions = meta.get("conditions") if isinstance(meta.get("conditions"), Mapping) else {}
    for key, value in raw_conditions.items():
        text_value = str(value).strip()
        if text_value and text_value.lower() != "unknown":
            conditions[str(key)] = text_value
    source = _clean_mapping(meta.get("source"))
    return Document(
        layer=layer,
        kind=kind,
        title=str(meta.get("title") or title),
        content=content,
        slug=str(meta.get("slug") or rel_slug),
        path=path,
        # The selected store owns identity. Historical sidecars cannot redirect
        # local files into another kind, layer or imported release.
        uri=uri_for(layer, rel, kind=kind),
        status=str(meta["status"]) if meta.get("status") else None,
        source=source,
        conditions=conditions,
        evidence=meta.get("evidence"),
        captured_at=meta.get("captured_at") if isinstance(meta.get("captured_at"), str) else None,
    )


def find_by_title(root: Path, title: str, *, layer: str, kind: str = "knowledge") -> Document | None:
    heading = (title or "").strip()
    if not heading or not root.is_dir():
        return None
    matches: list[Document] = []
    for path in iter_markdown_files(root, kind=kind):
        try:
            document = load_document(path, layer=layer, root=root, kind=kind)
        except (OSError, UnicodeDecodeError):
            continue
        if document.title.strip() == heading:
            matches.append(document)
    if not matches:
        return None
    wanted = document_slug(heading)
    for document in matches:
        if document.slug == wanted or document.path.stem == wanted:
            return document
    return matches[0]


def save_document(
    root: Path,
    *,
    layer: str,
    kind: str = "knowledge",
    title: str,
    content: str,
    slug: str | None = None,
    path: Path | None = None,
    status: str | None = None,
    source: Mapping[str, Any] | None = None,
    conditions: Mapping[str, Any] | None = None,
    evidence: Any = None,
    captured_at: str | None = None,
) -> Document:
    validate_kind(kind)
    heading = (title or "").strip()
    body = (content or "").strip()
    if not heading:
        raise ValueError("title is required")
    if not body:
        raise ValueError("content is required")
    if path is not None:
        target = Path(path)
        ident = target.stem
    else:
        ident = slug or document_slug(heading)
        target = root / f"{ident}.md"
    if not _in_store(target, root, kind):
        raise ValueError("document path is outside the selected content store")
    if path is None:
        if target.is_file():
            try:
                existing = load_document(target, layer=layer, root=root, kind=kind)
            except (OSError, UnicodeDecodeError):
                existing = None
            if existing is not None and existing.title.strip() != heading:
                ident = f"{ident}-{title_digest(heading + ident)}"
                target = root / f"{ident}.md"
    previous: dict[str, Any] = {}
    if meta_path(target).is_file():
        try:
            loaded = json.loads(meta_path(target).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            pass
    _atomic_write_text(target, render_markdown(heading, body))
    cleaned_source = _clean_mapping(source)
    cleaned_conditions: dict[str, str] = {}
    if isinstance(conditions, Mapping):
        for key, value in conditions.items():
            text_value = str(value).strip()
            if text_value and text_value.lower() != "unknown":
                cleaned_conditions[str(key)] = text_value
    rel = relative_posix(target, root)
    meta: dict[str, Any] = {
        **previous,
        "slug": ident,
        "title": heading,
        "layer": layer,
        "kind": validate_kind(kind),
        "uri": uri_for(layer, rel, kind=kind),
        "captured_at": captured_at or utc_now(),
    }
    if status is not None:
        meta["status"] = status
    if source is not None:
        if cleaned_source:
            meta["source"] = cleaned_source
        else:
            meta.pop("source", None)
    if conditions is not None:
        if cleaned_conditions:
            meta["conditions"] = cleaned_conditions
        else:
            meta.pop("conditions", None)
    if evidence is not None:
        meta["evidence"] = evidence
    _atomic_write_text(meta_path(target), json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return load_document(target, layer=layer, root=root, kind=kind)


def delete_document(path: Path) -> None:
    sidecar = meta_path(path)
    path.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)


def iter_markdown_files(root: Path, *, kind: str | None = None) -> list[Path]:
    if kind is not None:
        validate_kind(kind)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*.md")
        if path.is_file() and (kind is None or _in_store(path, root, kind))
    )
