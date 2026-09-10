"""Release adaptation: turn a build manifest + OVPack into a release layout.

This round produces and consumes a *local release directory* only::

    <release-dir>/
      release.json                 # validated release manifest
      <pack.file>                  # the dense OVPack asset

Real GitHub Release creation and network publishing are disabled here on
purpose; the corpus CI template uploads this directory as a workflow artifact
and the publishing step stays commented until the main agent wires it up.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vaws_knowledge.distribution.errors import ReleaseError, SourceUnavailable
from vaws_knowledge.distribution.manifest import (
    ExpectedContract,
    ReleaseManifest,
    atomic_write_json,
    read_json,
    sha256_file,
    validate_release_manifest,
)

RELEASE_MANIFEST_NAME = "release.json"


@dataclass
class ReleaseSnapshot:
    """A fetched release: validated manifest plus a local pack file to stage."""

    manifest: ReleaseManifest
    pack_path: Path
    label: str


class ReleaseSource(Protocol):
    def fetch(self) -> ReleaseSnapshot: ...


class LocalReleaseSource:
    """Reads a release directory produced by :func:`make_release`."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def fetch(self) -> ReleaseSnapshot:
        directory = self.directory
        manifest_path = directory / RELEASE_MANIFEST_NAME
        if not directory.is_dir():
            raise SourceUnavailable(
                f"release directory {directory} is not reachable; when pointing at a "
                "mirror, check the local path, or retry when back online"
            )
        data = read_json(manifest_path)
        if data is None:
            raise SourceUnavailable(
                f"{manifest_path} is missing or not valid JSON; the release is incomplete"
            )
        manifest = validate_release_manifest(data, expected=ExpectedContract())
        pack_path = directory / manifest.pack["file"]
        if not pack_path.is_file():
            raise SourceUnavailable(
                f"release asset {pack_path.name} is missing from {directory}"
            )
        return ReleaseSnapshot(manifest=manifest, pack_path=pack_path, label=str(directory))


def source_from_location(location: Any) -> ReleaseSource:
    """Resolve a configured source. Network sources are disabled this round."""

    if isinstance(location, LocalReleaseSource):
        return location
    text = str(location)
    if text.startswith(("http://", "https://")):
        raise SourceUnavailable(
            "network release sources are disabled in this round; mirror the release "
            "directory locally and point the source at that directory"
        )
    return LocalReleaseSource(Path(text))


def make_release(
    *,
    pack_path: Path,
    build_manifest: Path | dict[str, Any],
    out_dir: Path,
    expected: ExpectedContract | None = None,
) -> Path:
    """Assemble a local release directory from a build result.

    The build manifest is re-validated against the pinned contract before
    anything is published, and the pack copy is re-hashed, so a release
    directory is always self-consistent.
    """

    pack_path = Path(pack_path)
    out_dir = Path(out_dir)
    if isinstance(build_manifest, Path):
        data = read_json(build_manifest)
        if data is None:
            raise ReleaseError(f"build manifest {build_manifest} is missing or invalid JSON")
    else:
        data = dict(build_manifest)
    manifest = validate_release_manifest(data, expected=expected or ExpectedContract())
    if not pack_path.is_file():
        raise ReleaseError(f"pack {pack_path} does not exist")
    if sha256_file(pack_path) != manifest.pack["sha256"]:
        raise ReleaseError(
            f"pack {pack_path} sha256 does not match the build manifest; "
            "rebuild instead of mixing artifacts"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / manifest.pack["file"]
    if pack_path.resolve() != target.resolve():
        shutil.copyfile(pack_path, target)
    if sha256_file(target) != manifest.pack["sha256"]:
        raise ReleaseError(f"copied pack {target} failed its checksum; remove and retry")
    atomic_write_json(out_dir / RELEASE_MANIFEST_NAME, manifest.data)
    return out_dir


def publish_release(*_args: Any, **_kwargs: Any) -> None:
    """Real GitHub Release creation / network publishing — disabled this round."""

    raise ReleaseError(
        "real release creation and network publishing are disabled in this round; "
        "use make_release() to produce a local release directory and let the corpus "
        "CI template carry it as a workflow artifact"
    )
