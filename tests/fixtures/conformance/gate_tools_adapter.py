#!/usr/bin/env python3
"""Map tools/validate.py, tools/redact.py, tools/export.py or
bot/conflicts.py onto the gate verdict contract.

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
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


def _incomplete(message: str) -> int:
    sys.stderr.write(message.rstrip("\n") + "\n")
    return EXIT_USAGE


def run_schema(path: Path) -> int:
    try:
        from vaws_knowledge import validate
        from vaws_knowledge._common import ToolError
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
        from vaws_knowledge import redact
        from vaws_knowledge._common import ToolError
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


def run_conflicts(path: Path) -> int:
    """Reject when the corpus-wide conflict gate reports a blocking conflict.

    Unlike the other two this is a cross-entry gate: it is handed a whole
    document and answers about pairs inside it, so it goes through
    ``bot.corpus.load_paths`` rather than looking at one entry.
    """
    try:
        from vaws_knowledge.bot.conflicts import find_conflicts, today_utc
        from vaws_knowledge.bot.corpus import DependencyError, load_paths, repo_root
        from vaws_knowledge.bot.policy import PolicyError, load_policy
    except Exception as exc:
        return _incomplete(f"adapter: cannot import the conflicts gate: {exc}")

    try:
        policy = load_policy(None)
        loaded = load_paths([str(path)], repo_root())
        report = find_conflicts(loaded, policy, today_utc(), [])
    except (DependencyError, PolicyError, ValueError, OSError) as exc:
        return _incomplete(f"adapter: conflicts gate did not complete: {exc}")
    except Exception as exc:
        return _incomplete(f"adapter: conflicts gate did not complete: {exc}")

    if loaded.errors:
        return _incomplete(
            "adapter: conflicts gate could not load the document: "
            + "; ".join(f"{e.path}: {e.message}" for e in loaded.errors)
        )
    if not isinstance(report, dict) or "counts" not in report:
        return _incomplete("adapter: conflicts gate returned no report")

    for record in report.get("conflicts", []):
        sys.stderr.write(
            f"conflict {record['a']['uuid']} vs {record['b']['uuid']}: "
            f"{record.get('blocking_reason', '')}\n"
        )
    if report["counts"]["blocking"]:
        print("reject")
        return EXIT_FINDINGS
    print("accept")
    return EXIT_OK


def run_export(path: Path) -> int:
    """Write the real exporter's bytes to stdout, for the idempotence vectors.

    Not a verdict gate: the runner runs this twice and compares the two byte
    strings. It drives ``tools/export.py`` rather than a stub so that the
    property under test is the exporter's own determinism - including the key
    ordering of the ``measurement`` body, which a stub would never touch.

    ``submitted_at`` is pinned to the document's own ``updated_at`` instead of
    today. Otherwise the exporter stamps the wall clock, and two runs that
    straddle midnight UTC would differ for a reason that has nothing to do
    with the exporter being deterministic.
    """
    try:
        import yaml

        from vaws_knowledge import export
        from vaws_knowledge._common import ToolError
    except Exception as exc:
        return _incomplete(f"adapter: cannot import exporter: {exc}")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        stamp = None
        if isinstance(document, dict):
            stamp = document.get("updated_at")
        built = export.build_document(
            [str(path)],
            submitted_at=str(stamp) if stamp else "2026-01-01",
            today=str(stamp) if stamp else "2026-01-01",
            report=lambda message: None,
        )
        rendered = export.serialize(built)
    except export.ExportRefused as exc:
        return _incomplete(f"adapter: exporter refused the document: {exc}")
    except ToolError as exc:
        return _incomplete(f"adapter: exporter did not complete: {exc}")
    except Exception as exc:
        return _incomplete(f"adapter: exporter did not complete: {exc}")

    if not isinstance(rendered, str) or not rendered:
        return _incomplete("adapter: exporter produced no bytes")
    sys.stdout.write(rendered)
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "tool",
        choices=("schema", "redaction", "conflicts", "export", "validate", "redact"),
        help="schema/validate wraps tools.validate; redaction/redact wraps "
        "tools.redact.scan_file; conflicts wraps bot.conflicts.find_conflicts; "
        "export wraps tools.export.build_document + serialize",
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
        if args.tool == "conflicts":
            return run_conflicts(tmp_path)
        if args.tool == "export":
            return run_export(tmp_path)
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
