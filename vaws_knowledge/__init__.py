"""Local Markdown references, optional retrieval and public contribution."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from vaws_knowledge import corpus, redact

__all__ = ["corpus", "package_version", "redact"]


def package_version() -> str:
    """Installed distribution version. The package version is the contract."""

    try:
        return version("vaws-knowledge")
    except PackageNotFoundError:
        return "0.5.1"
