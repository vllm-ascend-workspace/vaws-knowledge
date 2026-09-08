"""Helpers shared by the tools CLIs.

Nothing here is part of the contract. It exists so that dependency errors,
YAML loading and path rendering read the same way in every tool.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

PACKAGE_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = PACKAGE_ROOT / "schemas" / "knowledge-v2.schema.json"

CORPUS_EXTENSIONS = (".yaml", ".yml", ".json")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


class ToolError(Exception):
    """A user-facing error. The CLI prints ``str(exc)`` and exits ``EXIT_USAGE``."""


def _dependency_message(module: str) -> str:
    return (
        f"error: the Python package '{module}' is not installed.\n"
        "  Install vaws-knowledge (which depends on it):\n"
        "    python3 -m pip install -e .\n"
    )


def require_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        raise ToolError(_dependency_message("PyYAML")) from None
    return yaml


def require_jsonschema():
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        raise ToolError(_dependency_message("jsonschema")) from None
    return jsonschema


def load_schema(path: Path | None = None) -> dict[str, Any]:
    schema_path = path or SCHEMA_PATH
    try:
        with schema_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ToolError(f"error: schema not found at {schema_path}") from None
    except json.JSONDecodeError as exc:
        raise ToolError(f"error: schema at {schema_path} is not valid JSON: {exc}") from None


def load_document(path: Path) -> Any:
    """Load one YAML or JSON file with the safe loader.

    JSON is a YAML subset, so a single loader is enough. Parse errors are
    reported with the file name; the caller decides whether that is fatal.
    """
    yaml = require_yaml()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"{path}: cannot read file: {exc.strerror}") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ToolError(f"{path}: not valid YAML/JSON: {exc}") from None


def iter_corpus_files(inputs: Sequence[str | os.PathLike[str]]) -> Iterator[Path]:
    """Yield corpus files under the given paths, in a stable order.

    Files are yielded sorted so that reports are deterministic. Dot-directories
    (e.g. ``.venv``) are skipped when walking a directory.
    """
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            files = sorted(
                p
                for p in path.rglob("*")
                if p.is_file()
                and p.suffix in CORPUS_EXTENSIONS
                and not any(part.startswith(".") for part in p.relative_to(path).parts)
            )
            yield from files
        elif path.is_file():
            yield path
        else:
            raise ToolError(f"{path}: no such file or directory")


def json_pointer(path: Iterable[Any]) -> str:
    """Render a path of keys/indices as ``entries[0].scope.torch.range.min``."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out or "<document>"


def relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def run_cli(main) -> None:
    """Run ``main(argv) -> int`` and turn ``ToolError`` into a clean message."""
    try:
        code = main(sys.argv[1:])
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        code = EXIT_USAGE
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


