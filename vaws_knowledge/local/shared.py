"""Active shared-version join point.

distribution.current_shared(state_root) will return None or
{source_git_sha, root_uri, manifest_path}. This module reads that result when
the distribution package is present, otherwise a local current.json. Search
must use the active root only so an imported new version cannot mix with an
old one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vaws_knowledge.markdown import URI_ROOT

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


def shared_search_uri(state_root: Path | None) -> str:
    current = current_shared(state_root)
    if current and current.get("root_uri"):
        return str(current["root_uri"])
    return DEFAULT_SHARED_URI
