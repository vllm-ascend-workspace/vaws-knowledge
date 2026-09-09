"""Mount configuration for the three knowledge layers.

The trust model in README.md has three layers, and a reader must be able to
tell which one an answer came from:

    shared     the packaged corpus (verified/ and unverified/), read-only
    project    a business repo's own knowledge, e.g. .agents/knowledge/
    candidate  a developer's untracked local capture directory

A layer is a trust source. Each entry carries its own ``status``; default
visibility is ``policy.default_statuses``. Unverified shared entries stay
hidden until a caller passes ``statuses`` or changes that policy.

A layer that is not configured, or a shared/project path that does not exist,
is *absent*. Candidate is different: it is a local write target, so a
configured root that has not been created yet is an empty mounted layer, not
a missing one. A candidate path that exists but cannot be read is still
absent. Every absence is reported with a reason so that a caller can tell
"this layer said nothing" apart from "this layer was not consulted".

Nothing here validates entries against schemas/knowledge-v2.schema.json --
that is the contributing fork's job (CONTRIBUTING.md) and the review bot's
job. This module only locates documents and reads them without raising.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:  # PyYAML is the only soft dependency of this package.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    yaml = None  # type: ignore

#: Ordered by trust, most trusted first. Also the tie-break order for
#: duplicate uuids: a reviewed shared entry shadows a local copy of itself.
LAYERS: tuple[str, ...] = ("shared", "project", "candidate")
LAYER_PRECEDENCE: dict[str, int] = {name: i for i, name in enumerate(LAYERS)}

#: The write path exists for exactly one layer. See capture.py.
WRITABLE_LAYERS = frozenset({"candidate"})

DOC_SUFFIXES = (".yaml", ".yml", ".json")

ENV_CONFIG = "VAWS_KNOWLEDGE_CONFIG"
ENV_CORPUS = "VAWS_KNOWLEDGE_CORPUS"
ENV_LAYER_ROOTS = {
    "shared": "VAWS_KNOWLEDGE_SHARED_ROOTS",
    "project": "VAWS_KNOWLEDGE_PROJECT_ROOTS",
    "candidate": "VAWS_KNOWLEDGE_CANDIDATE_ROOT",
}
ENV_ENABLED_LAYERS = "VAWS_KNOWLEDGE_LAYERS"
ENV_IDENTITY = {
    "contributor": "VAWS_KNOWLEDGE_CONTRIBUTOR",
    "origin_repo": "VAWS_KNOWLEDGE_ORIGIN_REPO",
    "redaction_profile": "VAWS_KNOWLEDGE_REDACTION_PROFILE",
}
ENV_STALE_AFTER_DAYS = "VAWS_KNOWLEDGE_STALE_AFTER_DAYS"

CONFIG_BASENAMES = ("vaws-knowledge.json", "vaws-knowledge.yaml", "vaws-knowledge.yml")

YAML_MISSING_HINT = (
    "PyYAML is not importable, so YAML knowledge documents cannot be read. "
    "Install it with: python3 -m pip install vaws-knowledge "
    "(JSON documents still load.)"
)

DEFAULT_IDENTITY = {
    "contributor": "anonymous",
    "origin_repo": "local/unpublished",
    "redaction_profile": "r1",
}

#: docs/lifecycle.md: verified and stale are returned by default, stale with a
#: warning; resolved is returned by default with its fix reference; unverified
#: and deprecated are not.
DEFAULT_STATUSES: tuple[str, ...] = ("verified", "stale", "resolved")
OPT_IN_STATUSES: tuple[str, ...] = ("unverified",)
SHARED_SUBSETS: tuple[str, ...] = ("verified", "unverified")
SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"

DEFAULT_POLICY: dict[str, Any] = {
    # docs/lifecycle.md leaves the horizon to policy. This only *labels* an
    # entry whose last_verified_at is older; it never rewrites status, because
    # the staleness sweep owns that transition.
    "stale_after_days": 180,
    "default_statuses": list(DEFAULT_STATUSES),
}


class ConfigError(Exception):
    """Raised only for a config file that cannot be interpreted at all."""


def repo_root() -> Path:
    """Directory containing this package. Not a corpus checkout."""

    return Path(__file__).resolve().parent.parent


def resolve_shared_from_corpus(raw: str | Path) -> tuple[Path, ...]:
    """Map a corpus root or repo checkout to shared subset directories.

    Returns every existing ``verified/`` and ``unverified/`` child. Layer is
    the trust source; entry ``status`` is a separate axis.
    """

    path = Path(raw).expanduser()
    for base in (path, path / "corpus"):
        roots = tuple(base / subset for subset in SHARED_SUBSETS if (base / subset).is_dir())
        if roots:
            return roots
    return (path / "verified",)


def default_shared_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    env = os.environ if env is None else env
    corpus = env.get(ENV_CORPUS)
    if corpus:
        return resolve_shared_from_corpus(corpus)
    from vaws_knowledge.corpus import corpus_root

    return resolve_shared_from_corpus(corpus_root())


def shared_source() -> dict[str, str | None]:
    """Pin the packaged corpus to the installed commons commit, when known."""

    from vaws_knowledge.corpus import installed_commit

    return {"source_ref": installed_commit(), "source_repo": SOURCE_REPO}


def default_candidate_root() -> Path:
    """Local, untracked capture directory.

    Deliberately outside the checkout: candidate notes are per-developer state,
    not repository content, and a default inside the repo would eventually be
    committed by somebody.
    """

    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "vaws-knowledge" / "candidate"


@dataclass(frozen=True)
class Mount:
    """One layer, resolved against the filesystem."""

    layer: str
    roots: tuple[Path, ...] = ()
    read_only: bool = True
    configured: bool = False
    present: bool = False
    absent_reason: str | None = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "layer": self.layer,
            "configured": self.configured,
            "present": self.present,
            "read_only": self.read_only,
            "roots": [str(p) for p in self.roots],
        }
        if self.absent_reason:
            out["absent_reason"] = self.absent_reason
        return out


@dataclass
class ServiceConfig:
    """Everything the query and capture paths need to run."""

    mounts: dict[str, Mount] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_IDENTITY))
    policy: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_POLICY))
    config_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def mount(self, layer: str) -> Mount:
        return self.mounts.get(layer, Mount(layer=layer, absent_reason="unknown layer"))

    def available_layers(self) -> list[str]:
        return [name for name in LAYERS if self.mounts.get(name, Mount(name)).present]

    def absent_layers(self, layers: Sequence[str] | None = None) -> dict[str, str]:
        wanted = [name for name in (layers or LAYERS) if name in LAYER_PRECEDENCE]
        out: dict[str, str] = {}
        for name in wanted:
            mount = self.mounts.get(name)
            if mount is None:
                out[name] = "not configured"
            elif not mount.present:
                out[name] = mount.absent_reason or "not present"
        return out

    def degraded(self, layers: Sequence[str] | None = None) -> bool:
        """True when a consulted layer is missing.

        Layers that were not requested do not affect completeness.
        """

        return bool(self.absent_layers(layers))

    def consulted(self, layers: Sequence[str] | None = None) -> dict[str, Any]:
        wanted = [name for name in (layers or LAYERS) if name in LAYER_PRECEDENCE]
        return {
            "layers_available": [name for name in wanted if self.mount(name).present],
            "layers_absent": self.absent_layers(wanted),
            "degraded": self.degraded(wanted),
        }

    def describe(self) -> dict[str, Any]:
        from vaws_knowledge import package_version

        out = {
            "version": package_version(),
            "config_path": str(self.config_path) if self.config_path else None,
            "layers": {name: self.mount(name).describe() for name in LAYERS},
            "layers_available": self.available_layers(),
            "layers_absent": self.absent_layers(),
            "degraded": self.degraded(),
            "identity": dict(self.identity),
            "policy": dict(self.policy),
            "yaml_available": yaml is not None,
            "warnings": list(self.warnings),
        }
        out.update(shared_source())
        return out

    @property
    def stale_after_days(self) -> int:
        try:
            return int(self.policy.get("stale_after_days", 180))
        except (TypeError, ValueError):
            return 180

    @property
    def default_statuses(self) -> tuple[str, ...]:
        raw = self.policy.get("default_statuses") or DEFAULT_STATUSES
        if isinstance(raw, str):
            raw = [raw]
        return tuple(str(item) for item in raw)


# --------------------------------------------------------------------------
# config loading
# --------------------------------------------------------------------------


def _read_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    if yaml is None:
        raise ConfigError(f"{path.name}: {YAML_MISSING_HINT}")
    return yaml.safe_load(text)


def find_config_file(env: Mapping[str, str], start: Path | None = None) -> Path | None:
    """Locate a config file: explicit env var first, then well-known names."""

    explicit = env.get(ENV_CONFIG)
    if explicit:
        return Path(explicit).expanduser()

    candidates: list[Path] = []
    cwd = start or Path.cwd()
    xdg = env.get("XDG_CONFIG_HOME")
    config_home = Path(xdg) if xdg else Path.home() / ".config"
    for basename in CONFIG_BASENAMES:
        candidates.append(cwd / basename)
        candidates.append(cwd / ".vaws" / basename)
        candidates.append(config_home / "vaws-knowledge" / basename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _split_paths(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        for piece in chunk.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


def _resolve(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def _candidate_root_problem(path: Path) -> str | None:
    """None when the root is an empty-or-readable write target.

    A path that has never been created is the normal initial state. A path
    that exists but cannot be listed is a real gap.
    """

    try:
        if not path.exists():
            return None
        if not path.is_dir():
            return f"path is not a directory: {path}"
        os.listdir(path)
    except OSError as exc:
        return f"cannot read {path}: {exc}"
    return None


def _candidate_mount(
    roots: tuple[Path, ...],
    *,
    read_only: bool,
    configured: bool,
) -> Mount:
    problems = [reason for path in roots if (reason := _candidate_root_problem(path))]
    if problems and len(problems) == len(roots):
        return Mount(
            layer="candidate",
            roots=roots,
            read_only=read_only,
            configured=True,
            present=False,
            absent_reason="; ".join(problems),
        )
    return Mount(
        layer="candidate",
        roots=roots,
        read_only=read_only,
        configured=configured or True,
        present=True,
        absent_reason=None if not problems else "some configured roots cannot be read: " + "; ".join(problems),
    )


def _layer_spec(raw: Any) -> dict[str, Any]:
    """Normalize the several shapes a layer may be written in."""

    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"roots": [raw]}
    if isinstance(raw, (list, tuple)):
        return {"roots": list(raw)}
    if isinstance(raw, Mapping):
        spec = dict(raw)
        roots = spec.pop("roots", None)
        single = spec.pop("root", None)
        collected: list[str] = []
        if isinstance(roots, str):
            collected.append(roots)
        elif isinstance(roots, (list, tuple)):
            collected.extend(str(item) for item in roots)
        if isinstance(single, str):
            collected.append(single)
        spec["roots"] = collected
        return spec
    raise ConfigError(f"layer configuration must be a string, list or mapping, got {type(raw).__name__}")


def load_config(
    mapping: Mapping[str, Any] | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> ServiceConfig:
    """Build a :class:`ServiceConfig`.

    Precedence, lowest first: built-in defaults, config file, ``mapping``,
    environment variables. Relative paths resolve against the config file's
    directory when there is one, otherwise against ``base_dir`` or the
    checkout root -- never against the process cwd, which changes under a
    server.

    Never raises for a missing or absent layer. Only a config file that cannot
    be parsed at all raises :class:`ConfigError`.
    """

    env = dict(os.environ if env is None else env)
    warnings: list[str] = []

    config_path: Path | None = None
    file_data: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path).expanduser()
    elif mapping is None:
        config_path = find_config_file(env, start=Path(base_dir) if base_dir else None)

    if config_path is not None:
        if config_path.is_file():
            try:
                loaded = _read_structured(config_path)
            except ConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, Mapping):
                raise ConfigError(f"config {config_path} must contain a mapping")
            file_data = dict(loaded)
        else:
            warnings.append(f"config file not found, using defaults: {config_path}")
            config_path = None

    data: dict[str, Any] = dict(file_data)
    if mapping:
        for key, value in mapping.items():
            if key == "layers" and isinstance(value, Mapping) and isinstance(data.get("layers"), Mapping):
                merged = dict(data["layers"])
                merged.update(value)
                data["layers"] = merged
            else:
                data[key] = value

    if base_dir is not None:
        base = Path(base_dir).expanduser()
    elif config_path is not None:
        base = config_path.parent
    else:
        base = repo_root()

    identity = dict(DEFAULT_IDENTITY)
    for key, value in (data.get("identity") or {}).items():
        if value is not None:
            identity[str(key)] = str(value)
    for key, var in ENV_IDENTITY.items():
        if env.get(var):
            identity[key] = env[var]

    policy = dict(DEFAULT_POLICY)
    for key, value in (data.get("policy") or {}).items():
        if value is not None:
            policy[str(key)] = value
    if env.get(ENV_STALE_AFTER_DAYS):
        try:
            policy["stale_after_days"] = int(env[ENV_STALE_AFTER_DAYS])
        except ValueError:
            warnings.append(f"{ENV_STALE_AFTER_DAYS} is not an integer; keeping {policy['stale_after_days']}")

    layers_cfg_raw = data.get("layers") or {}
    if not isinstance(layers_cfg_raw, Mapping):
        raise ConfigError("'layers' must be a mapping of layer name to configuration")

    enabled_filter: set[str] | None = None
    if env.get(ENV_ENABLED_LAYERS) is not None:
        enabled_filter = {name.strip() for name in env[ENV_ENABLED_LAYERS].split(",") if name.strip()}

    for unknown in set(layers_cfg_raw) - set(LAYERS):
        warnings.append(f"ignoring unknown layer in config: {unknown}")

    mounts: dict[str, Mount] = {}
    for layer in LAYERS:
        try:
            spec = _layer_spec(layers_cfg_raw.get(layer))
        except ConfigError as exc:
            warnings.append(f"{layer}: {exc}; treating layer as unconfigured")
            spec = {}

        roots_raw = [str(item) for item in spec.get("roots", []) if str(item).strip()]
        configured = bool(roots_raw) or "enabled" in spec

        env_var = ENV_LAYER_ROOTS[layer]
        if env_var in env:
            env_value = env[env_var]
            if env_value.strip() == "":
                mounts[layer] = Mount(
                    layer=layer,
                    configured=True,
                    read_only=layer not in WRITABLE_LAYERS,
                    absent_reason=f"disabled by {env_var} (empty value)",
                )
                continue
            roots_raw = _split_paths(env_value)
            configured = True

        enabled = spec.get("enabled", True)
        if enabled_filter is not None and layer not in enabled_filter:
            mounts[layer] = Mount(
                layer=layer,
                configured=configured,
                read_only=layer not in WRITABLE_LAYERS,
                absent_reason=f"disabled by {ENV_ENABLED_LAYERS}",
            )
            continue
        if not enabled:
            mounts[layer] = Mount(
                layer=layer,
                configured=True,
                read_only=layer not in WRITABLE_LAYERS,
                absent_reason="disabled in configuration",
            )
            continue

        if not roots_raw:
            if layer == "shared":
                roots = default_shared_roots(env)
            elif layer == "candidate":
                roots = (default_candidate_root(),)
            else:
                mounts[layer] = Mount(
                    layer=layer,
                    configured=False,
                    read_only=True,
                    absent_reason="not configured (no roots supplied)",
                )
                continue
        else:
            roots = tuple(_resolve(item, base) for item in roots_raw)

        existing = tuple(p for p in roots if p.is_dir())
        # shared is read-only by contract; docs/federation.md forbids a fork
        # writing into verified/. candidate is the only writable layer.
        read_only = bool(spec.get("read_only", layer not in WRITABLE_LAYERS))
        if layer != "candidate":
            read_only = True

        if layer == "candidate":
            mounts[layer] = _candidate_mount(roots, read_only=read_only, configured=configured)
            continue

        if existing:
            mounts[layer] = Mount(
                layer=layer,
                roots=existing,
                read_only=read_only,
                configured=configured or layer == "shared",
                present=True,
                absent_reason=None
                if len(existing) == len(roots)
                else "some configured roots do not exist: "
                + ", ".join(str(p) for p in roots if p not in existing),
            )
        else:
            mounts[layer] = Mount(
                layer=layer,
                roots=roots,
                read_only=read_only,
                configured=configured or layer == "shared",
                present=False,
                absent_reason="path does not exist: " + ", ".join(str(p) for p in roots),
            )

    if yaml is None:
        warnings.append(YAML_MISSING_HINT)

    return ServiceConfig(
        mounts=mounts,
        identity=identity,
        policy=policy,
        config_path=config_path,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# document loading
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedEntry:
    """One entry, plus where it came from."""

    layer: str
    entry: Mapping[str, Any]
    kind: str
    document_layer: str
    document_updated_at: str | None
    root: str
    source: str  # path relative to its root, so results stay portable

    @property
    def uuid(self) -> str:
        return str(self.entry.get("uuid", ""))

    @property
    def status(self) -> str:
        return str(self.entry.get("status", "unverified"))

    def origin(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "kind": self.kind,
            "document_layer": self.document_layer,
            "file": self.source,
        }


@dataclass
class LoadReport:
    entries: list[LoadedEntry] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    scanned_files: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "scanned_files": self.scanned_files,
            "errors": list(self.errors),
        }


def iter_document_files(mount: Mount) -> Iterator[tuple[Path, Path]]:
    """Yield ``(root, file)`` for every knowledge document under a mount."""

    for root in mount.roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in DOC_SUFFIXES:
                yield root, path


def load_entries(
    config: ServiceConfig,
    layers: Sequence[str] | None = None,
) -> LoadReport:
    """Read every entry from the requested (and present) layers.

    A malformed document is recorded in ``report.errors`` and skipped. One bad
    file must not take the service down, and it must not silently disappear
    either -- a caller that cannot see the error would read an incomplete
    result set as "no such fact".
    """

    wanted = [name for name in (layers or LAYERS) if name in LAYER_PRECEDENCE]
    report = LoadReport()

    for layer in wanted:
        mount = config.mount(layer)
        if not mount.present:
            continue
        for root, path in iter_document_files(mount):
            report.scanned_files += 1
            rel = str(path.relative_to(root))
            try:
                doc = _read_structured(path)
            except ConfigError as exc:
                report.errors.append({"layer": layer, "file": rel, "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 - one bad file is not fatal
                report.errors.append(
                    {"layer": layer, "file": rel, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue

            if not isinstance(doc, Mapping):
                report.errors.append(
                    {"layer": layer, "file": rel, "error": "document root is not a mapping"}
                )
                continue
            entries = doc.get("entries")
            if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, Mapping)):
                report.errors.append(
                    {"layer": layer, "file": rel, "error": "document has no 'entries' list"}
                )
                continue

            kind = str(doc.get("kind", "unknown"))
            document_layer = str(doc.get("layer", "unknown"))
            updated_at = doc.get("updated_at")
            for item in entries:
                if not isinstance(item, Mapping):
                    report.errors.append(
                        {"layer": layer, "file": rel, "error": "entry is not a mapping"}
                    )
                    continue
                if not item.get("uuid"):
                    report.errors.append(
                        {"layer": layer, "file": rel, "error": "entry has no uuid; skipped"}
                    )
                    continue
                report.entries.append(
                    LoadedEntry(
                        layer=layer,
                        entry=item,
                        kind=kind,
                        document_layer=document_layer,
                        document_updated_at=str(updated_at) if updated_at else None,
                        root=str(root),
                        source=rel,
                    )
                )

    return report
