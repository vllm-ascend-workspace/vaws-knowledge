#!/usr/bin/env python3
"""Map tools/validate.py or tools/redact.py onto the gate verdict contract.

The real tools take a file path, not stdin, and they print findings rather
than `accept`/`reject`. This adapter is the documented recipe: write stdin
to a temporary file in the tool's actual input format, call the tool's
Python API, and print one verdict token only after that call returns a
completed structured result. An import, RuntimeError, OSError, ToolError,
or any other execution failure prints no token so the runner records a
protocol failure rather than a semantic verdict.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


def _incomplete(message: str) -> int:
    sys.stderr.write(message.rstrip("\n") + "\n")
    return EXIT_USAGE


def run_schema(path: Path) -> int:
    try:
        from tools import validate
        from tools._common import ToolError
    except Exception as exc:
        return _incomplete(f"adapter: cannot import validator: {exc}")

    try:
        result = validate.validate_paths([str(path)])
    except ToolError as exc:
        return _incomplete(f"adapter: validator did not complete: {exc}")
    except (ImportError, RuntimeError, OSError) as exc:
        return _incomplete(f"adapter: validator did not complete: {exc}")
    except Exception as exc:
        return _incomplete(f"adapter: validator did not complete: {exc}")

    if not isinstance(result, validate.ValidationResult):
        return _incomplete("adapter: validator returned no ValidationResult")

    for problem in result.problems:
        sys.stderr.write(problem.render() + "\n")
    if result.ok:
        print("accept")
        return EXIT_OK
    print("reject")
    return EXIT_FINDINGS


def run_redaction(path: Path) -> int:
    try:
        from tools import redact
        from tools._common import ToolError
    except Exception as exc:
        return _incomplete(f"adapter: cannot import redactor: {exc}")

    try:
        findings = redact.scan_file(path)
    except ToolError as exc:
        return _incomplete(f"adapter: redactor did not complete: {exc}")
    except (ImportError, RuntimeError, OSError) as exc:
        return _incomplete(f"adapter: redactor did not complete: {exc}")
    except Exception as exc:
        return _incomplete(f"adapter: redactor did not complete: {exc}")

    if not isinstance(findings, list):
        return _incomplete("adapter: redactor returned no findings list")

    for finding in findings:
        sys.stderr.write(finding.render() + "\n")
    if findings:
        print("reject")
        return EXIT_FINDINGS
    print("accept")
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "tool",
        choices=("schema", "redaction", "validate", "redact"),
        help="schema/validate wraps tools.validate; redaction/redact wraps "
        "tools.redact.scan_file",
    )
    args = parser.parse_args(argv)

    data = sys.stdin.buffer.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="vaws-gate-", suffix=".yaml", delete=False
        ) as handle:
            handle.write(data)
            tmp_path = Path(handle.name)
        if args.tool in ("schema", "validate"):
            return run_schema(tmp_path)
        return run_redaction(tmp_path)
    except OSError as exc:
        return _incomplete(f"adapter could not use temporary input: {exc}")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
