"""Locate the corpus that ships in the ``vaws-knowledge`` wheel.

Checkout layout stays ``corpus/`` at the repository root. The wheel maps that
tree to ``vaws_knowledge/data/corpus`` so this module and the data directory
do not share a path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGED_ROOT = _PACKAGE_DIR / "data" / "corpus"
_CHECKOUT_ROOT = _PACKAGE_DIR.parent / "corpus"
_YAML_SUFFIXES = (".yaml", ".yml")
DEFAULT_SUBSETS = ("verified", "unverified")


def corpus_root() -> Path:
    """Return the packaged corpus directory, or the checkout ``corpus/``."""

    if _PACKAGED_ROOT.is_dir():
        return _PACKAGED_ROOT
    if _CHECKOUT_ROOT.is_dir():
        return _CHECKOUT_ROOT
    return _PACKAGED_ROOT


def iter_entry_files(
    subsets: tuple[str, ...] = DEFAULT_SUBSETS,
) -> Iterator[Path]:
    """Yield YAML entry files under ``corpus_root() / subset``, sorted."""

    root = corpus_root()
    files: list[Path] = []
    for subset in subsets:
        directory = root / subset
        if not directory.is_dir():
            continue
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix in _YAML_SUFFIXES
        )
    yield from sorted(files)


def installed_commit() -> str | None:
    """Git commit of the installed distribution, or ``None`` if unknown."""

    try:
        dist = metadata.distribution("vaws-knowledge")
    except metadata.PackageNotFoundError:
        return None
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    vcs = data.get("vcs_info")
    if not isinstance(vcs, dict):
        return None
    commit_id = vcs.get("commit_id")
    return commit_id if isinstance(commit_id, str) and commit_id else None
