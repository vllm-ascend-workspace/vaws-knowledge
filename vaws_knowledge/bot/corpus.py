"""Load knowledge-v2 documents into a flat, deterministic list of entries.

This module does *not* validate against the schema; that is
``tools/validate.py``'s job and the bot shells out to it. Loading here is
tolerant: malformed files are reported as load errors (which fail closed
upstream) and malformed entries are still yielded so that the gates can point
at them by path.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from vaws_knowledge.canonical import BODY_KEYS

try:  # pragma: no cover - exercised only when the dependency is missing
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None


class DependencyError(RuntimeError):
    """A required third-party dependency is not importable."""


DEPENDENCY_HINT = (
    "bot: PyYAML is not installed. Install the package:\n"
    "    python3 -m pip install -e ."
)


def require_yaml() -> Any:
    if yaml is None:
        raise DependencyError(DEPENDENCY_HINT)
    return yaml


SCOPE_DIMENSIONS: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "python_abi",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
    "model",
    "topology",
    "execution_mode",
    "component",
)

RULE_BODY_FIELDS: tuple[str, ...] = ("summary", "symptom", "root_cause", "resolution")

YAML_SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class EntryRef:
    """One entry plus enough location data to point a reviewer at it."""

    path: str
    doc_index: int
    entry_index: int
    kind: str
    layer: str
    entry: Mapping[str, Any]

    # -- identity -----------------------------------------------------------
    @property
    def uuid(self) -> str:
        return _string(self.entry.get("uuid"))

    @property
    def slug(self) -> str:
        return _string(self.entry.get("slug"))

    @property
    def content_hash(self) -> str:
        return _string(self.entry.get("content_hash"))

    @property
    def status(self) -> str:
        return _string(self.entry.get("status"))

    @property
    def confidence(self) -> str:
        return _string(self.entry.get("confidence"))

    # -- sub-objects --------------------------------------------------------
    @property
    def scope(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("scope"))

    @property
    def rule(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("rule"))

    @property
    def measurement(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("measurement"))

    @property
    def body(self) -> str:
        """``rule`` / ``measurement`` / ``reference`` / ``none`` / ``both``.

        Reported rather than raised: loading is tolerant so that a malformed
        entry can still be pointed at by path. ``bot/integrity.py`` turns
        ``none`` and ``both`` into findings.
        """
        present = [key for key in BODY_KEYS if key in self.entry]
        if len(present) == 1:
            return present[0]
        return "both" if present else "none"

    @property
    def is_measurement(self) -> bool:
        return self.body == "measurement"

    @property
    def is_reference(self) -> bool:
        return self.body == "reference"

    @property
    def reference(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("reference"))

    @property
    def subject_id(self) -> str:
        """Identity of what a measurement is about; empty for a rule or reference."""
        return _string(_mapping(self.measurement.get("subject")).get("id")).strip()

    @property
    def method_type(self) -> str:
        return _string(_mapping(self.measurement.get("method")).get("type")).strip()

    def quantities(self) -> dict[tuple[str, str], tuple[str, str]]:
        """``{(name, basis): (value, unit)}`` for a measurement entry.

        ``(name, basis)`` is the quantity identity and ``(value, unit)`` is the
        claim. Keying on ``basis`` as well as ``name`` is deliberate: a
        theoretical peak and a sustained fraction of it are different claims
        about the same physical thing, so they must never be compared as
        though one contradicted the other.
        """
        raw = self.measurement.get("quantities")
        if not isinstance(raw, list):
            return {}
        out: dict[tuple[str, str], tuple[str, str]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            key = (_string(item.get("name")).strip(), _string(item.get("basis")).strip())
            if not key[0]:
                continue
            out[key] = (_string(item.get("value")).strip(), _string(item.get("unit")).strip())
        return out

    @property
    def lifecycle(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("lifecycle"))

    @property
    def verification(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("verification"))

    @property
    def provenance(self) -> Mapping[str, Any]:
        return _mapping(self.entry.get("provenance"))

    @property
    def conflicts(self) -> Sequence[Mapping[str, Any]]:
        raw = self.entry.get("conflicts")
        if not isinstance(raw, list):
            return ()
        return tuple(c for c in raw if isinstance(c, Mapping))

    @property
    def fingerprints(self) -> tuple[str, ...]:
        raw = self.rule.get("fingerprints")
        if not isinstance(raw, list):
            return ()
        return tuple(str(f) for f in raw if isinstance(f, (str, int, float)))

    @property
    def location(self) -> str:
        return f"{self.path}#entries[{self.entry_index}]"

    @property
    def in_verified_dir(self) -> bool:
        return self.path.startswith("corpus/verified/")

    @property
    def in_unverified_dir(self) -> bool:
        return self.path.startswith("corpus/unverified/")

    def rule_body(self) -> str:
        """The prose a text-similarity comparison may read.

        Empty for a measurement or reference entry, and deliberately so. A
        measurement's summary and method description are near-identical across
        an entire vendor catalogue — sixty-three platform_config rows differ
        only in a SoC name and some numbers — so scoring them as prose would
        report the whole catalogue as duplicates of itself. A sourced
        reference is compared on its citation, not on an empty rule.
        Measurements are compared on subject, quantity identity and
        coordinate instead; see ``measurement_body_key`` and ``bot/dedup.py``.
        """
        if self.is_measurement or self.is_reference:
            return ""
        parts = [_string(self.rule.get(f)) for f in RULE_BODY_FIELDS]
        return "\n".join(p for p in parts if p)

    def measurement_body_key(self) -> tuple:
        """Structural identity of a measurement claim, for exact comparison.

        Subject, method type and the full sorted quantity set. Two entries
        with the same key at the same coordinate are the same claim; two with
        the same subject but a different value for a shared quantity are a
        contradiction.
        """
        return (
            self.subject_id.lower(),
            self.method_type,
            tuple(sorted((name, basis, value, unit)
                         for (name, basis), (value, unit) in self.quantities().items())),
        )

    def describe(self) -> dict[str, str]:
        """Compact, deterministic pointer used in every gate's JSON output."""
        return {
            "uuid": self.uuid,
            "slug": self.slug,
            "status": self.status,
            "location": self.location,
        }

    def sort_key(self) -> tuple[str, int, int]:
        return (self.path, self.doc_index, self.entry_index)


@dataclass
class LoadError:
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass
class LoadResult:
    entries: list[EntryRef] = field(default_factory=list)
    errors: list[LoadError] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_uuid(self) -> dict[str, list[EntryRef]]:
        out: dict[str, list[EntryRef]] = {}
        for ref in self.entries:
            out.setdefault(ref.uuid, []).append(ref)
        return out


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def repo_root(start: Path | None = None) -> Path:
    """Locate the corpus checkout: nearest ancestor with ``pyproject.toml`` + ``corpus/``, or ``.git``.

    Falls back to the current working directory. Never returns a path derived
    from user input outside the tree, so reports stay relative and portable.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "corpus").is_dir():
            return candidate
        if (candidate / ".git").exists():
            return candidate
    return here


def relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        try:
            return os.path.relpath(path.resolve(), root.resolve()).replace(os.sep, "/")
        except ValueError:
            # A caller may select a document from a different Windows drive.
            return path.resolve().as_posix()


def discover_yaml(paths: Iterable[str | Path], root: Path) -> list[Path]:
    """Expand files/directories into a sorted list of YAML files.

    Hidden directories (``.git``, ``.venv``) are skipped. Non-existent paths
    are returned as-is so the caller can report them as load errors.
    """
    found: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if p.is_dir():
            for child in p.rglob("*"):
                if child.is_file() and child.suffix in YAML_SUFFIXES:
                    if any(part.startswith(".") for part in child.relative_to(p).parts):
                        continue
                    found.add(child)
        else:
            found.add(p)
    return sorted(found, key=lambda x: relpath(x, root))


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_paths(paths: Iterable[str | Path], root: Path | None = None) -> LoadResult:
    root = root or repo_root()
    result = LoadResult()
    for file in discover_yaml(paths, root):
        rel = relpath(file, root)
        if not file.is_file():
            result.errors.append(LoadError(rel, "path does not exist"))
            continue
        result.files.append(rel)
        try:
            docs = list(_load_documents(file))
        except DependencyError:
            raise
        except Exception as exc:  # noqa: BLE001 - report, fail closed upstream
            result.errors.append(LoadError(rel, f"YAML parse error: {_one_line(exc)}"))
            continue
        for doc_index, doc in enumerate(docs):
            if doc is None:
                continue
            if not isinstance(doc, Mapping):
                result.errors.append(
                    LoadError(rel, f"document {doc_index} is not a mapping")
                )
                continue
            entries = doc.get("entries")
            if not isinstance(entries, list):
                result.errors.append(
                    LoadError(rel, f"document {doc_index} has no `entries` list")
                )
                continue
            kind = _string(doc.get("kind"))
            layer = _string(doc.get("layer"))
            for entry_index, entry in enumerate(entries):
                if not isinstance(entry, Mapping):
                    result.errors.append(
                        LoadError(rel, f"entries[{entry_index}] is not a mapping")
                    )
                    continue
                result.entries.append(
                    EntryRef(rel, doc_index, entry_index, kind, layer, entry)
                )
    result.entries.sort(key=EntryRef.sort_key)
    result.errors.sort(key=lambda e: (e.path, e.message))
    return result


def _load_documents(file: Path) -> Iterator[Any]:
    y = require_yaml()
    with file.open("r", encoding="utf-8") as fh:
        yield from y.safe_load_all(fh)


# ---------------------------------------------------------------------------
# small helpers shared by the gates
# ---------------------------------------------------------------------------


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def constraint_kind(constraint: Any) -> str:
    """Classify a scope constraint: ``any`` / ``values`` / ``range`` / ``invalid``."""
    if not isinstance(constraint, Mapping):
        return "invalid"
    if constraint.get("any") is True:
        return "any"
    if isinstance(constraint.get("values"), list) and constraint["values"]:
        return "values"
    rng = constraint.get("range")
    if isinstance(rng, Mapping) and "min" in rng and "max" in rng:
        return "range"
    return "invalid"


def normalized_values(constraint: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        _string(v).strip().lower() for v in constraint.get("values", []) if _string(v).strip()
    )


def pair_key(a: EntryRef, b: EntryRef) -> tuple[str, str]:
    """Order a pair deterministically by (uuid, location)."""
    ka = (a.uuid, a.location)
    kb = (b.uuid, b.location)
    return (a.location, b.location) if ka <= kb else (b.location, a.location)


def ordered_pair(a: EntryRef, b: EntryRef) -> tuple[EntryRef, EntryRef]:
    return (a, b) if (a.uuid, a.location) <= (b.uuid, b.location) else (b, a)
