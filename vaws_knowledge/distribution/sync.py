"""Local shared-version sync: fetch, verify, import into staging URI, switch.

The periodic caller (the existing knowledge service lifecycle) runs
:func:`check_and_sync`; there is no new OS timer or daemon. No change returns
quietly. Any failure leaves ``current.json`` pointing at the old version, and
staging is owned by this component so a later run can clean it safely.

Platform rules: the switch pointer is updated with fsync + os.replace; the
single-switcher lock is an atomically created file with a liveness/staleness
check — no POSIX-only flock/fork, and no database file is ever overwritten in
place (a new version imports into its own URI subtree).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from vaws_knowledge.distribution.client import MetricsReader, connect_client, metrics_delta
from vaws_knowledge.distribution.errors import (
    CorruptPack,
    DistributionError,
    ImportFailed,
    IncompatiblePack,
    SourceUnavailable,
    SwitchInProgress,
)
from vaws_knowledge.distribution.manifest import (
    ExpectedContract,
    ReleaseManifest,
    atomic_write_json,
    read_json,
    shared_root_uri,
    utc_now,
    validate_release_manifest,
)
from vaws_knowledge.distribution.pack import verify_model_files, verify_pack
from vaws_knowledge.distribution.release import ReleaseSource, source_from_location

CURRENT_SCHEMA = "vaws-knowledge-current-shared/1"


@dataclass
class SyncResult:
    """Outcome of one check. ``status`` is one of unchanged/switched/busy/
    offline/corrupt/incompatible/error; ``reason`` stays empty on success."""

    status: str
    reason: str = ""
    version_id: str | None = None
    source_git_sha: str | None = None
    root_uri: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"unchanged", "switched"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "version_id": self.version_id,
            "source_git_sha": self.source_git_sha,
            "root_uri": self.root_uri,
            "details": self.details,
        }


class DistributionState:
    """The ``distribution/`` subtree of the knowledge state root."""

    def __init__(self, state_root: Path):
        self.root = Path(state_root) / "distribution"

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def lock_path(self) -> Path:
        return self.root / "sync.lock"

    @property
    def last_sync_path(self) -> Path:
        return self.root / "last-sync.json"

    def read_current(self) -> dict[str, Any] | None:
        payload = read_json(self.current_path)
        if payload is None:
            return None
        if payload.get("schema") != CURRENT_SCHEMA:
            return None
        for key in ("version_id", "source_git_sha", "root_uri", "manifest_path"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                return None
        return payload

    def write_current(self, payload: Mapping[str, Any]) -> None:
        atomic_write_json(self.current_path, payload)

    def version_dir(self, version_id: str) -> Path:
        return self.versions_dir / version_id

    def record(self, result: SyncResult) -> None:
        payload = result.to_dict()
        payload["at"] = utc_now()
        try:
            atomic_write_json(self.last_sync_path, payload)
        except OSError:
            pass


def current_shared(state_root: Path) -> dict[str, str] | None:
    """The active shared version, or ``None``.

    Returns exactly ``{source_git_sha, root_uri, manifest_path}`` so the local
    backend can search the active shared root; a missing/corrupt pointer means
    "no shared version", never a crash for existing queries.
    """

    payload = DistributionState(state_root).read_current()
    if payload is None:
        return None
    return {
        "source_git_sha": payload["source_git_sha"],
        "root_uri": payload["root_uri"],
        "manifest_path": payload["manifest_path"],
    }


def _lock_file_nb(fd: int) -> None:
    """Non-blocking exclusive lock on the open file; raises OSError when held."""

    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class SwitchLock:
    """Single-active-switcher lock anchored on one persistent file.

    The lock itself is OS-managed (``fcntl.flock`` on POSIX, ``msvcrt.locking``
    on Windows) and the OS releases it when the holder exits or crashes, so a
    live holder stays exclusive for *any* duration and there is no stale
    metadata to heuristically reclaim. The file is deliberately never
    unlinked: an old owner's ``release`` only closes its own descriptor and
    cannot delete a later acquisition. The JSON payload is diagnostic only —
    liveness is never inferred from it, so a half-written payload during
    another process's create→write window just yields a conservative ``busy``.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.acquired = False
        self._fd: int | None = None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt" and os.fstat(fd).st_size == 0:
                os.write(fd, b" ")  # msvcrt.locking needs byte 0 to exist
            _lock_file_nb(fd)
        except OSError:
            os.close(fd)
            holder = read_json(self.path) or {}
            raise SwitchInProgress(
                "another switch is in progress "
                f"(pid {holder.get('pid', '?')} since {holder.get('at', '?')}); "
                "the next periodic check will retry"
            ) from None
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": os.uname().nodename if hasattr(os, "uname") else "",
                "at": time.time(),
            }
        )
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload.encode("utf-8"))
        except OSError:
            pass  # diagnostic only; the OS lock is what excludes
        self._fd = fd
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        fd, self._fd = self._fd, None
        self.acquired = False
        try:
            _unlock_file(fd)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self) -> "SwitchLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def _uri_exists(client: Any, uri: str) -> bool:
    try:
        client.stat(uri)
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if "not found" in text or "not_found" in text or "404" in text:
            return False
        raise
    return True


def _stage_download(snapshot: Any, staging: Path) -> Path:
    """Copy the pack into the isolated staging dir; hash-checked by verify_pack."""

    staging.mkdir(parents=True, exist_ok=False)
    target = staging / snapshot.manifest.pack["file"]
    try:
        shutil.copyfile(snapshot.pack_path, target)
    except OSError as exc:
        raise SourceUnavailable(
            f"cannot stage {snapshot.pack_path} into {staging}: {exc}"
        ) from None
    return target


def _clean_staging(state: DistributionState) -> None:
    root = state.staging_dir
    if not root.is_dir():
        return
    for child in root.iterdir():
        resolved = child.resolve()
        if root.resolve() not in resolved.parents:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _prune_versions(
    state: DistributionState,
    *,
    client: Any,
    keep_inactive: int,
    details: dict[str, Any],
) -> None:
    """Remove non-active versions this component owns, beyond the keep window."""

    if not state.versions_dir.is_dir():
        return
    current = state.read_current() or {}
    keep_id = current.get("version_id")
    entries: list[tuple[float, str]] = []
    for child in state.versions_dir.iterdir():
        if not child.is_dir() or child.name == keep_id:
            continue
        activated = read_json(child / "activated.json") or {}
        try:
            at = float(activated.get("at_epoch") or 0)
        except (TypeError, ValueError):
            at = 0.0
        entries.append((at, child.name))
    entries.sort(reverse=True)
    pruned: list[str] = []
    deferred: list[str] = []
    for _at, version_id in entries[ max(keep_inactive, 0) :]:
        root_uri = shared_root_uri(version_id)
        removed_remote = False
        if client is not None:
            try:
                if _uri_exists(client, root_uri):
                    client.rm(root_uri, recursive=True, wait=True, timeout=600)
                removed_remote = True
            except Exception:  # noqa: BLE001
                removed_remote = False
        if removed_remote or client is None:
            if removed_remote:
                shutil.rmtree(state.version_dir(version_id), ignore_errors=True)
                pruned.append(version_id)
            else:
                # Without a live client we cannot drop the imported subtree;
                # keep the record so a later run finishes the cleanup.
                deferred.append(version_id)
        else:
            deferred.append(version_id)
    if pruned:
        details["pruned_versions"] = pruned
    if deferred:
        details["prune_deferred"] = deferred


def check_and_sync(
    state_root: Path,
    source: Any,
    *,
    embedding_info: Mapping[str, Any],
    client: Any = None,
    client_factory: Callable[[], Any] | None = None,
    openviking_url: str | None = None,
    api_key: str | None = None,
    metrics_reader: MetricsReader | None = None,
    model_cache: Path | None = None,
    keep_inactive: int = 1,
    smoke_query: str | None = None,
) -> SyncResult:
    """One periodic check: fetch -> verify -> import new version -> switch.

    Never raises for expected failure modes; the previous version stays active
    and the result carries an actionable reason.
    """

    state = DistributionState(state_root)
    state.versions_dir.mkdir(parents=True, exist_ok=True)
    state.staging_dir.mkdir(parents=True, exist_ok=True)

    def finish(result: SyncResult) -> SyncResult:
        state.record(result)
        return result

    try:
        resolved = source if hasattr(source, "fetch") else source_from_location(source)
        snapshot = resolved.fetch()
    except SourceUnavailable as exc:
        return finish(SyncResult(status="offline", reason=exc.reason))
    except CorruptPack as exc:
        return finish(SyncResult(status="corrupt", reason=exc.reason))
    except IncompatiblePack as exc:
        return finish(SyncResult(status="incompatible", reason=exc.reason))

    manifest = snapshot.manifest
    version_id = manifest.version_id
    sha = manifest.source_git_sha
    root_uri = shared_root_uri(version_id)
    base = {"version_id": version_id, "source_git_sha": sha, "root_uri": root_uri}

    current = state.read_current()
    if current and current["source_git_sha"] == sha and current["root_uri"] == root_uri:
        return finish(SyncResult(status="unchanged", **base))

    try:
        contract = ExpectedContract.from_embedding_info(embedding_info)
    except IncompatiblePack as exc:
        return finish(SyncResult(status="incompatible", reason=exc.reason, **base))

    try:
        # The source validated against the pinned defaults; re-validate against
        # the actual live embedding identity of this machine.
        manifest = validate_release_manifest(manifest.data, expected=contract)
    except (CorruptPack, IncompatiblePack) as exc:
        status = "incompatible" if isinstance(exc, IncompatiblePack) else "corrupt"
        return finish(SyncResult(status=status, reason=exc.reason, **base))

    if model_cache is not None:
        problems = verify_model_files(model_cache, manifest)
        if problems:
            return finish(
                SyncResult(
                    status="incompatible",
                    reason="; ".join(problems),
                    **base,
                )
            )

    lock = SwitchLock(state.lock_path)
    try:
        lock.acquire()
    except SwitchInProgress as exc:
        return finish(SyncResult(status="busy", reason=exc.reason, **base))

    staging = state.staging_dir / f"{version_id}.{secrets.token_hex(4)}"
    details: dict[str, Any] = {"source": snapshot.label}
    try:
        _clean_staging(state)
        pack_path = _stage_download(snapshot, staging)
        try:
            info = verify_pack(pack_path, manifest, expected=contract)
        except (CorruptPack, IncompatiblePack) as exc:
            status = "incompatible" if isinstance(exc, IncompatiblePack) else "corrupt"
            return finish(SyncResult(status=status, reason=exc.reason, **base, details=details))
        details["pack"] = {
            "sha256": manifest.pack["sha256"],
            "size": manifest.pack["size"],
            "dense_records": info.dense.get("count"),
        }

        close_client = False
        if client is None:
            if client_factory is not None:
                client = client_factory()
            elif openviking_url:
                client = connect_client(openviking_url, api_key=api_key)
                close_client = True
            if client is None:
                raise ImportFailed(
                    "no live OpenViking instance was provided; start the local "
                    "knowledge service and retry"
                )
        try:
            import_details = _import_version(
                client,
                pack_path=pack_path,
                root_uri=root_uri,
                manifest=manifest,
                metrics_reader=metrics_reader,
                smoke_query=smoke_query,
            )
        except Exception:
            if close_client and client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            raise
        details.update(import_details)

        version_dir = state.version_dir(version_id)
        version_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(version_dir / "release.json", manifest.data)
        atomic_write_json(
            version_dir / "activated.json",
            {"version_id": version_id, "source_git_sha": sha, "at": utc_now(), "at_epoch": time.time()},
        )
        state.write_current(
            {
                "schema": CURRENT_SCHEMA,
                "version_id": version_id,
                "source_git_sha": sha,
                "root_uri": root_uri,
                "manifest_path": str(version_dir / "release.json"),
                "openviking_version": contract.openviking_version,
                "embedding": {
                    "model": contract.embedding_model,
                    "dimension": contract.embedding_dimension,
                },
                "activated_at": utc_now(),
            }
        )
        _prune_versions(state, client=client, keep_inactive=keep_inactive, details=details)
        if close_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return finish(SyncResult(status="switched", details=details, **base))
    except ImportFailed as exc:
        return finish(SyncResult(status="error", reason=exc.reason, details=details, **base))
    except DistributionError as exc:
        return finish(SyncResult(status="error", reason=exc.reason, details=details, **base))
    except Exception as exc:  # noqa: BLE001 - unexpected; keep old version, report faithfully
        return finish(
            SyncResult(
                status="error",
                reason=f"unexpected {type(exc).__name__}: {exc}",
                details=details,
                **base,
            )
        )
    finally:
        lock.release()
        shutil.rmtree(staging, ignore_errors=True)


def _import_version(
    client: Any,
    *,
    pack_path: Path,
    root_uri: str,
    manifest: ReleaseManifest,
    metrics_reader: MetricsReader | None,
    smoke_query: str | None,
) -> dict[str, Any]:
    """Native import with prebuilt vectors; old version stays active throughout."""

    details: dict[str, Any] = {}
    resumed = False
    try:
        if _uri_exists(client, root_uri):
            consistency = client.check_consistency(uri=root_uri)
            if consistency.get("ok"):
                resumed = True
            else:
                # Leftover of an interrupted attempt: drop it and import fresh.
                client.rm(root_uri, recursive=True, wait=True, timeout=600)
    except Exception as exc:  # noqa: BLE001
        raise ImportFailed(
            f"cannot inspect the target version root {root_uri}: {type(exc).__name__}: {exc}"
        ) from None

    if resumed:
        details["import_resumed"] = True
    else:
        # Parent-root bookkeeping (a freshly mkdir'd `shared` dir embeds its own
        # directory record) is instance initialization, not document import:
        # let it drain, then measure the import window alone.
        prepare_before = metrics_reader() if metrics_reader else None
        try:
            parent_uri = root_uri.rsplit("/", 1)[0]
            try:
                client.mkdir(parent_uri)
            except Exception as exc:  # noqa: BLE001 - existing parent is fine
                text = str(exc).upper()
                if "ALREADY" not in text and "EXISTS" not in text:
                    raise
            client.wait_processed(timeout=300)
        except Exception as exc:  # noqa: BLE001
            raise ImportFailed(
                f"cannot prepare the shared parent root for {root_uri}: "
                f"{type(exc).__name__}: {exc}"
            ) from None
        prepare_after = metrics_reader() if metrics_reader else None
        prepare_calls = metrics_delta(prepare_before, prepare_after)
        if prepare_calls:
            details["prepare_embedding_calls"] = prepare_calls

        before = metrics_reader() if metrics_reader else None
        started = time.monotonic()
        try:
            imported_uri = client.import_ovpack(
                str(pack_path), parent_uri, on_conflict="fail", vector_mode="require"
            )
            queues = client.wait_processed(timeout=1800)
            details["import_s"] = round(time.monotonic() - started, 3)
            if imported_uri and str(imported_uri).rstrip("/") != root_uri:
                raise ImportFailed(
                    f"native import landed at {imported_uri}, expected {root_uri}; "
                    "not switching to an unexpected root"
                )
            problems: list[str] = []
            for queue, state in (queues or {}).items():
                if isinstance(state, Mapping) and int(state.get("error_count") or 0):
                    problems.append(f"{queue}: {state.get('error_count')}")
            if problems:
                raise ImportFailed(
                    "native processing reported errors after import: " + ", ".join(problems)
                )
            after = metrics_reader() if metrics_reader else None
            calls = metrics_delta(before, after)
            details["import_embedding_calls"] = calls
            if calls.get("texts", 0) != 0 or calls.get("generation_rejected", 0) != 0:
                raise ImportFailed(
                    f"import triggered {calls.get('texts', 0)} embedding texts / "
                    f"{calls.get('generation_rejected', 0)} generation requests; "
                    "vector-mode=require must not recompute document embeddings"
                )
        except ImportFailed:
            # The import happened but failed verification: remove the rejected
            # subtree so a retry re-imports instead of resuming into it.
            try:
                client.rm(root_uri, recursive=True, wait=True, timeout=600)
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImportFailed(
                f"native import failed ({type(exc).__name__}: {exc}); "
                "the previous version remains active"
            ) from None

    consistency = client.check_consistency(uri=root_uri)
    if not consistency.get("ok"):
        raise ImportFailed(f"post-import consistency check failed: {consistency}")
    dense_count = (manifest.pack.get("index") or {}).get("dense", {}).get("count")
    expected_count = consistency.get("expected_count")
    if isinstance(dense_count, int) and isinstance(expected_count, int):
        if expected_count != dense_count:
            raise ImportFailed(
                f"imported record count {expected_count} != pack dense count {dense_count}"
            )
        details["imported_records"] = expected_count

    if smoke_query:
        before = metrics_reader() if metrics_reader else None
        try:
            payload = client.find(
                smoke_query,
                target_uri=root_uri,
                limit=3,
                options={"level": 2, "read_content": False, "score_threshold": 0},
            )
        except Exception as exc:  # noqa: BLE001
            raise ImportFailed(
                f"smoke query on the imported version failed: {type(exc).__name__}: {exc}"
            ) from None
        hits = payload.get("resources") if isinstance(payload, dict) else None
        details["smoke_query_hits"] = len(hits) if isinstance(hits, list) else 0
        after = metrics_reader() if metrics_reader else None
        query_calls = metrics_delta(before, after)
        if query_calls:
            details["query_embedding_calls"] = query_calls
    return details
