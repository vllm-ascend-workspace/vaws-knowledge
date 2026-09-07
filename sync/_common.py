"""Shared mechanics for the federation sync commands.

Everything here is deliberately boring: loading and dumping corpus documents,
the exact `content_hash` canonicalization from docs/federation.md, the
per-uuid corpus index, and the shell-out gates to `tools/`. The commands in
`sync/` compose these; none of them contain a code path that removes an entry.

Deletion is not a sync operation (docs/federation.md, "Deletion"). The single
write primitive, `write_documents`, refuses to write a corpus state in which
any previously present `uuid` is missing. That guard is structural, not a
convention, so that no future command in this package can delete by accident.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Callable, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on broken installs
    sys.stderr.write(
        "PyYAML is required by sync/. Install it with:\n"
        "    python3 -m pip install -r sync/requirements.txt\n"
    )
    raise SystemExit(2)


LAYERS = ("verified", "unverified")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PROFILE_RE = re.compile(r"^r([0-9]+)$")

# Exit codes shared by every command in sync/.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE = 2

# Key order used when writing documents. Anything not listed is appended in
# alphabetical order. Deterministic output is what makes `publish` byte-stable
# and what makes two forks inserting the same uuid collide in git instead of
# landing twice.
DOCUMENT_KEY_ORDER = ("schema_version", "kind", "layer", "updated_at", "entries")
ENTRY_KEY_ORDER = (
    "uuid",
    "slug",
    "content_hash",
    "status",
    "confidence",
    "scope",
    "provenance",
    "verification",
    "lifecycle",
    "conflicts",
    "rule",
)
SCOPE_KEY_ORDER = (
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
RULE_KEY_ORDER = ("summary", "symptom", "root_cause", "resolution", "avoidance", "fingerprints")


class SyncError(Exception):
    """A condition the sync code refuses to proceed past."""


class IntegrityError(SyncError):
    """The corpus on disk violates an invariant sync depends on."""


# --------------------------------------------------------------------------
# dates


def today(override: str | None = None) -> str:
    if override:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", override):
            raise SyncError(f"--today must be YYYY-MM-DD, got {override!r}")
        return override
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def normalize_dates(obj: Any) -> Any:
    """YAML resolves an unquoted 2026-09-07 to a date object. The schema wants
    strings, and the hash must not depend on how a producer quoted its dates."""
    if isinstance(obj, dict):
        return {k: normalize_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_dates(v) for v in obj]
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()[:10]
    return obj


# --------------------------------------------------------------------------
# canonicalization (docs/federation.md, "Idempotency", steps 0-5)
#
# NOTE: this duplicates what tools/canonical.py is meant to own. It exists
# because sync/ must not import from tools/ and cannot guess that tool's CLI.
# It is pinned against examples/valid-entry.yaml by tests/test_sync_canonical.py
# and should be collapsed onto tools/canonical.py once that CLI is stable.
# Do not stringify fingerprint items, mapping keys, or numeric bounds.


ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ASCII_WS_RUN = re.compile(r"[ \t\n\r\x0b\x0c]+")


def _ascii_lower(value: str) -> str:
    return value.translate(_ASCII_LOWER)


def _norm_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip(ASCII_WHITESPACE) for line in text.split("\n"))
    return text.strip(ASCII_WHITESPACE)


def _canon_strings(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, sub in obj.items():
            if not isinstance(key, str):
                raise SyncError(
                    f"mapping keys must be strings, got {type(key).__name__} {key!r}; "
                    "canonicalization does not stringify keys"
                )
            out[key] = _canon_strings(sub)
        return out
    if isinstance(obj, list):
        return [_canon_strings(v) for v in obj]
    if isinstance(obj, str):
        return _norm_text(obj)
    return obj


def _payload_type_error(value: Any, path: str, *, in_fingerprints: bool = False) -> str | None:
    if isinstance(value, dict):
        for key, sub in value.items():
            if not isinstance(key, str):
                return (
                    f"{path}: mapping keys must be strings, got {type(key).__name__} "
                    f"{key!r}; canonicalization does not stringify keys"
                )
            child = f"{path}.{key}"
            if key == "fingerprints" and not isinstance(sub, list):
                return (
                    f"{child} must be a list of strings, got {type(sub).__name__}; "
                    "canonicalization does not stringify fingerprint items"
                )
            err = _payload_type_error(sub, child, in_fingerprints=(key == "fingerprints"))
            if err:
                return err
        return None
    if isinstance(value, list):
        if in_fingerprints:
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    return (
                        f"{path}[{index}]: fingerprint items must be strings, got "
                        f"{type(item).__name__} {item!r}; canonicalization does not "
                        "stringify them"
                    )
            return None
        for index, item in enumerate(value):
            err = _payload_type_error(item, f"{path}[{index}]")
            if err:
                return err
        return None
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    if isinstance(value, (int, float)):
        return (
            f"{path}: numeric value {value!r} is a {type(value).__name__} and is not "
            "canonicalized; quote it as a string (canonicalization does not stringify types)"
        )
    return (
        f"{path}: unsupported type {type(value).__name__}; "
        "canonicalization does not coerce it"
    )


def canonical_fingerprints(items: Iterable[Any]) -> Any:
    if not isinstance(items, list):
        return items
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            return list(items)
        item = _ASCII_WS_RUN.sub(" ", _ascii_lower(item).strip(ASCII_WHITESPACE))
        if item:
            seen.add(item)
    return sorted(seen, key=lambda s: s.encode("utf-8"))


def canonical_payload(entry: dict) -> dict:
    rule_in = entry.get("rule", {})
    scope_in = entry.get("scope", {})
    for label, node in (("rule", rule_in), ("scope", scope_in)):
        if isinstance(node, dict):
            err = _payload_type_error(node, label)
            if err:
                raise SyncError(err)
    rule = _canon_strings(rule_in)
    if isinstance(rule, dict) and "fingerprints" in rule:
        fps = None
        if isinstance(entry.get("rule"), dict):
            fps = entry["rule"].get("fingerprints")
        rule["fingerprints"] = canonical_fingerprints(fps)
    scope = _canon_strings(scope_in)
    return {"rule": rule, "scope": scope}


def canonical_json(entry: dict) -> str:
    return json.dumps(
        canonical_payload(entry), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def content_hash(entry: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# redaction profile versions


def profile_number(profile: Any) -> int | None:
    if not isinstance(profile, str):
        return None
    m = PROFILE_RE.match(profile)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# YAML in / out


def load_yaml(path: pathlib.Path) -> Any:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SyncError(f"{path}: not valid YAML: {exc}") from exc
    return normalize_dates(data)


def _deep_sorted(obj: Any) -> Any:
    """Sort mapping keys recursively. List order is semantic (values,
    fingerprints, evidence) and is preserved."""
    if isinstance(obj, dict):
        return {k: _deep_sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_deep_sorted(v) for v in obj]
    return obj


def _ordered(obj: Any, order: tuple[str, ...]) -> dict:
    keys = [k for k in order if k in obj] + sorted(k for k in obj if k not in order)
    return {k: _deep_sorted(obj[k]) for k in keys}


def order_entry(entry: dict) -> dict:
    out = _ordered(entry, ENTRY_KEY_ORDER)
    if isinstance(out.get("scope"), dict):
        out["scope"] = _ordered(out["scope"], SCOPE_KEY_ORDER)
    if isinstance(out.get("rule"), dict):
        out["rule"] = _ordered(out["rule"], RULE_KEY_ORDER)
    return out


def order_document(doc: dict) -> dict:
    out = _ordered(doc, DOCUMENT_KEY_ORDER)
    entries = sorted(out.get("entries") or [], key=lambda e: str(e.get("uuid", "")))
    out["entries"] = [order_entry(e) for e in entries]
    return out


def dump_yaml(doc: dict) -> str:
    return yaml.safe_dump(
        order_document(doc),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


# --------------------------------------------------------------------------
# corpus model


@dataclasses.dataclass
class Located:
    layer: str
    kind: str
    path: pathlib.Path
    entry: dict


@dataclasses.dataclass
class Document:
    layer: str
    kind: str
    path: pathlib.Path
    data: dict


@dataclasses.dataclass
class Corpus:
    root: pathlib.Path
    documents: dict[pathlib.Path, Document]
    index: dict[str, Located]

    def uuids(self) -> set[str]:
        return set(self.index)

    def document_for(self, layer: str, kind: str) -> Document | None:
        candidates = sorted(
            (d for d in self.documents.values() if d.layer == layer and d.kind == kind),
            key=lambda d: d.path,
        )
        return candidates[0] if candidates else None

    def target_path(self, layer: str, kind: str) -> pathlib.Path:
        existing = self.document_for(layer, kind)
        if existing:
            return existing.path
        return self.root / layer / f"{kind}.yaml"


def _iter_corpus_files(root: pathlib.Path, layer: str) -> list[pathlib.Path]:
    layer_dir = root / layer
    if not layer_dir.is_dir():
        return []
    return sorted(p for p in layer_dir.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file())


def load_corpus(root: pathlib.Path) -> Corpus:
    """Load corpus/{verified,unverified}. Fails on any integrity violation:
    layer not matching the directory, a uuid present twice, a malformed id."""
    root = pathlib.Path(root).resolve()
    if not root.is_dir():
        raise SyncError(f"corpus directory does not exist: {root}")
    documents: dict[pathlib.Path, Document] = {}
    index: dict[str, Located] = {}
    for layer in LAYERS:
        for path in _iter_corpus_files(root, layer):
            data = load_yaml(path)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise IntegrityError(f"{path}: not a v2 knowledge document")
            if data.get("layer") != layer:
                raise IntegrityError(
                    f"{path}: layer={data.get('layer')!r} but the file lives in corpus/{layer}/"
                )
            kind = data.get("kind")
            if not isinstance(kind, str) or not kind:
                raise IntegrityError(f"{path}: missing kind")
            documents[path] = Document(layer=layer, kind=kind, path=path, data=data)
            for entry in data["entries"]:
                uid = entry.get("uuid")
                if not isinstance(uid, str) or not UUID_RE.match(uid):
                    raise IntegrityError(f"{path}: malformed uuid {uid!r}")
                if uid in index:
                    other = index[uid]
                    raise IntegrityError(
                        f"uuid {uid} appears twice: {other.path} and {path}. "
                        "Identity is one uuid per corpus; fix this before syncing."
                    )
                index[uid] = Located(layer=layer, kind=kind, path=path, entry=entry)
    return Corpus(root=root, documents=documents, index=index)


def upsert_entry(doc: dict, entry: dict) -> None:
    """Insert or replace by uuid. There is intentionally no remove counterpart."""
    entries = doc.setdefault("entries", [])
    for i, existing in enumerate(entries):
        if existing.get("uuid") == entry["uuid"]:
            entries[i] = entry
            return
    entries.append(entry)


def _uuids_in(doc: dict) -> set[str]:
    return {e.get("uuid") for e in doc.get("entries", [])}


def write_documents(
    corpus: Corpus, changes: dict[pathlib.Path, dict], *, dry_run: bool = False
) -> list[pathlib.Path]:
    """Write changed documents. Refuses if any uuid previously present in the
    corpus would be absent afterwards — deletion is not a sync operation."""
    before = corpus.uuids()
    after = set(before)
    for path, doc in changes.items():
        prior = corpus.documents.get(path)
        prior_uuids = _uuids_in(prior.data) if prior else set()
        new_uuids = _uuids_in(doc)
        after -= prior_uuids
        after |= new_uuids
    missing = before - after
    if missing:
        raise IntegrityError(
            "refusing to write: these entries would disappear from the corpus: "
            + ", ".join(sorted(missing))
            + ". Entries are resolved or deprecated, never removed by sync."
        )
    written: list[pathlib.Path] = []
    for path, doc in sorted(changes.items()):
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_yaml(doc), encoding="utf-8")
        written.append(path)
    return written


# --------------------------------------------------------------------------
# export documents (what a fork's tools/export.py produces)


def load_export(path: pathlib.Path) -> dict:
    data = load_yaml(pathlib.Path(path))
    if not isinstance(data, dict):
        raise SyncError(f"{path}: export is not a mapping")
    if data.get("schema_version") != 2:
        raise SyncError(f"{path}: schema_version must be 2, got {data.get('schema_version')!r}")
    if not isinstance(data.get("kind"), str) or not data["kind"]:
        raise SyncError(f"{path}: export has no kind")
    if not isinstance(data.get("entries"), list):
        raise SyncError(f"{path}: export has no entries list")
    return data


# --------------------------------------------------------------------------
# gates: shell out to tools/, fail closed when absent


@dataclasses.dataclass
class GateResult:
    name: str
    status: str  # passed | failed | unavailable | skipped
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("passed", "skipped")


Runner = Callable[..., subprocess.CompletedProcess]


def default_runner(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found ({exc})")


def run_gate(
    tools_dir: pathlib.Path,
    tool: str,
    args: list[str],
    *,
    runner: Runner = default_runner,
    cwd: pathlib.Path | None = None,
) -> GateResult:
    script = pathlib.Path(tools_dir) / tool
    if not script.is_file():
        return GateResult(
            name=tool,
            status="unavailable",
            detail=f"gate unavailable: {script} does not exist; refusing to proceed without it",
        )
    proc = runner([sys.executable, str(script), *args], cwd=str(cwd) if cwd else None)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return GateResult(name=tool, status="passed", detail=output.strip())
    return GateResult(
        name=tool, status="failed", detail=f"exit {proc.returncode}\n{output.strip()}"
    )


def run_source_gates(
    tools_dir: pathlib.Path,
    paths: list[pathlib.Path],
    *,
    runner: Runner = default_runner,
    skip: bool = False,
) -> list[GateResult]:
    """The two gates a fork must pass before anything is proposed."""
    if skip:
        return [
            GateResult("validate.py", "skipped", "skipped by --skip-gates"),
            GateResult("redact.py", "skipped", "skipped by --skip-gates"),
        ]
    str_paths = [str(p) for p in paths]
    return [
        run_gate(tools_dir, "validate.py", str_paths, runner=runner),
        run_gate(tools_dir, "redact.py", ["--check", *str_paths], runner=runner),
    ]


def gates_summary(results: list[GateResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"gate {r.name}: {r.status}")
        if r.detail and r.status != "passed":
            for line in r.detail.splitlines():
                lines.append(f"    {line}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# misc


def repo_root_from(path: pathlib.Path | None) -> pathlib.Path:
    if path is not None:
        return pathlib.Path(path).resolve()
    return pathlib.Path(__file__).resolve().parent.parent


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")
