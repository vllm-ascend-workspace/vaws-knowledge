"""Active shared-version join point.

distribution.current_shared(state_root) will return None or
{source_git_sha, root_uri, manifest_path}. This module reads that result when
the distribution package is present, otherwise a local current.json. Search
uses the local bootstrap and the active release as separate roots. It never
searches their shared parent, which can also contain inactive releases.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from vaws_knowledge.markdown import SHARED_BOOTSTRAP_URI, URI_ROOT, validate_kind

DEFAULT_SHARED_URI = f"{URI_ROOT}/shared"


def current_shared(state_root: Path | None) -> dict[str, Any] | None:
    if state_root is None:
        return None
    root = Path(state_root)
    imported = None
    try:
        from vaws_knowledge.distribution import current_shared as imported_current

        imported = imported_current
    except Exception:  # noqa: BLE001 - distribution is a later batch
        imported = None
    if imported is not None:
        try:
            payload = imported(root)
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict) and payload.get("root_uri"):
            return {
                "source_git_sha": payload.get("source_git_sha"),
                "root_uri": payload.get("root_uri"),
                "manifest_path": payload.get("manifest_path"),
            }
    path = root / "shared" / "current.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("root_uri"):
        return None
    return {
        "source_git_sha": data.get("source_git_sha"),
        "root_uri": data.get("root_uri"),
        "manifest_path": data.get("manifest_path"),
    }


def shared_search_uri(state_root: Path | None, *, kind: str = "knowledge") -> str:
    """The active release root, or bootstrap when no safe release is active."""

    return shared_search_uris(state_root, kind=kind)[-1]


def matches_targets(uri: str, targets: tuple[str, ...] | list[str]) -> bool:
    """Directories include descendants; manifest-selected files match exactly."""

    if any(part in {".", ".."} for part in uri.split("/")):
        return False
    return any(uri == root or (not root.endswith(".md") and uri.startswith(root + "/")) for root in targets)


def active_shared_uris(current: dict[str, Any] | None, *, kind: str = "knowledge") -> tuple[str, ...]:
    """Select a kind before ranking, including old packs with untyped paths.

    An old pack's manifest supplies exact document targets. Searching the
    version parent would mix the two kinds and consume the wrong top-k slots.
    Missing manifests permit only the explicitly typed subtree.
    """

    validate_kind(kind)
    active = str(current.get("root_uri") or "").rstrip("/") if current else ""
    # Releases are direct children, or one exact repair generation. Reject
    # broad parents rather than retrieving inactive versions or bootstrap a
    # second time. The repair layout is owned by distribution.sync.
    prefix = DEFAULT_SHARED_URI + "/"
    version = active[len(prefix):] if active.startswith(prefix) else ""
    direct = version and version not in {".", "..", "bootstrap", "repairs"} and "/" not in version
    repair = re.fullmatch(r"repairs/[0-9a-f]{16}/v[0-9a-f]{12}", version)
    if not (direct or repair):
        return ()
    manifest_path = current.get("manifest_path") if current else None
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8")) if manifest_path else {}
    except (OSError, ValueError, TypeError):
        manifest = {}
    content = manifest.get("content", {}) if isinstance(manifest, dict) else {}
    if not isinstance(content, dict) or content.get("layout") == "kinds/v1":
        return (f"{active}/{kind}",)
    entries = content.get("files")
    if not isinstance(entries, list):
        return (f"{active}/{kind}",)
    targets: list[str] = []
    for entry in entries:
        path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or "\\" in path or any(p in {".", ".."} for p in path.split("/")) or not path.endswith(".md"):
            continue
        recorded_kind = parts[0] if parts[0] in {"knowledge", "experience"} else "knowledge"
        if recorded_kind == kind:
            targets.append(f"{active}/{path}")
    return tuple(dict.fromkeys(targets))


def shared_search_uris(state_root: Path | None, *, kind: str = "knowledge") -> tuple[str, ...]:
    """Return disjoint bootstrap and active-release targets for one kind."""

    validate_kind(kind)
    return (f"{SHARED_BOOTSTRAP_URI}/{kind}", *active_shared_uris(current_shared(state_root), kind=kind))


def shared_search_problem(current: dict[str, Any] | None) -> str | None:
    """Expose incomplete legacy scope when the active manifest was lost."""

    if not current:
        return None
    path = current.get("manifest_path")
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
        if isinstance(manifest, dict) and isinstance(manifest.get("content", {}).get("files"), list):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "The active shared manifest is unavailable; only explicit knowledge/experience subtrees can be searched."
