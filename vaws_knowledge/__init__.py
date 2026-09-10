"""Installable engine for the vaws-knowledge commons.

The public library surface is ``canonical``, ``validate``, ``redact``,
``export`` and ``corpus``. The corpus ships in the wheel.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from vaws_knowledge import canonical, corpus, export, redact, validate

__all__ = ["canonical", "corpus", "export", "package_version", "redact", "validate"]


def package_version() -> str:
    """Installed distribution version. The package version is the contract."""

    try:
        return version("vaws-knowledge")
    except PackageNotFoundError:
        return "0.3.1"
