"""Console entry for ``vaws-knowledge`` / ``python -m vaws_knowledge``."""

from __future__ import annotations

import argparse
import sys
from typing import Callable


def _dispatch(handler: Callable[[list[str]], int], argv: list[str]) -> int:
    from vaws_knowledge._common import ToolError, EXIT_USAGE

    try:
        return handler(argv)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="vaws-knowledge",
        description="Engine CLI for the vaws-knowledge commons. The corpus ships in the wheel.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "server",
            "validate",
            "redact",
            "export",
            "canonical",
            "query",
            "capture",
            "contribution",
            "distribution",
            "conformance",
        ),
        help="subcommand",
    )
    parser.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="arguments forwarded to the subcommand",
    )
    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    if argv[0] == "help":
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    command = args.command
    rest = list(args.rest)
    if rest[:1] == ["--"]:
        rest = rest[1:]

    if command == "server":
        from vaws_knowledge.server.mcp_server import main as server_main

        return server_main(rest)
    if command == "validate":
        from vaws_knowledge.validate import main as validate_main

        return _dispatch(validate_main, rest)
    if command == "redact":
        from vaws_knowledge.redact import main as redact_main

        return _dispatch(redact_main, rest)
    if command == "export":
        from vaws_knowledge.export import main as export_main

        return _dispatch(export_main, rest)
    if command == "canonical":
        from vaws_knowledge.canonical import main as canonical_main

        return _dispatch(canonical_main, rest)
    if command == "query":
        from vaws_knowledge.server.query import main as query_main

        return query_main(rest)
    if command == "capture":
        from vaws_knowledge.server.capture_cli import main as capture_main

        return _dispatch(capture_main, rest)
    if command == "contribution":
        from vaws_knowledge.contribution.__main__ import main as contribution_main

        return contribution_main(rest)
    if command == "distribution":
        from vaws_knowledge.distribution.__main__ import main as distribution_main

        return distribution_main(rest)
    if command == "conformance":
        from vaws_knowledge.conformance.runner import main as conformance_main

        return conformance_main(rest)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
