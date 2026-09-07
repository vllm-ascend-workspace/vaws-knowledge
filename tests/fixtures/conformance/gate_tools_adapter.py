#!/usr/bin/env python3
"""Map tools/validate.py or tools/redact.py onto the gate verdict contract.

The real tools take a file path, not stdin, and they print findings rather
than `accept`/`reject`. This adapter is the documented recipe: write stdin to
a temporary file in the tool's actual input format, invoke the tool, print
one verdict token, and propagate execution failures without a token so the
runner records a protocol failure rather than a semantic verdict.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
VALIDATE = REPO / "tools" / "validate.py"
REDACT = REPO / "tools" / "redact.py"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "tool",
        choices=("schema", "redaction", "validate", "redact"),
        help="schema/validate wraps tools/validate.py; redaction/redact wraps "
        "tools/redact.py --check",
    )
    args = parser.parse_args(argv)

    if args.tool in ("schema", "validate"):
        command = [sys.executable, str(VALIDATE), "-q"]
    else:
        command = [sys.executable, str(REDACT), "--check"]

    data = sys.stdin.buffer.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="vaws-gate-", suffix=".yaml", delete=False
        ) as handle:
            handle.write(data)
            tmp_path = Path(handle.name)
        try:
            proc = subprocess.run(
                command + [str(tmp_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            sys.stderr.write(f"adapter could not start tool: {exc}\n")
            return 127
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    # Findings belong on stderr so they cannot look like a verdict token.
    if proc.stdout:
        sys.stderr.buffer.write(proc.stdout)
        if not proc.stdout.endswith(b"\n"):
            sys.stderr.buffer.write(b"\n")
    if proc.stderr:
        sys.stderr.buffer.write(proc.stderr)

    if proc.returncode == 0:
        print("accept")
        return 0
    if proc.returncode == 1:
        print("reject")
        return 1
    return proc.returncode if proc.returncode else 2


if __name__ == "__main__":
    raise SystemExit(main())
