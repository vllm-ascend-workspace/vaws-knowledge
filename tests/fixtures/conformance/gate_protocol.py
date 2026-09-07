#!/usr/bin/env python3
"""Configurable gate used to prove protocol failures are not verdicts.

The runner must not infer accept/reject from exit status. This fixture can
emit a token, sleep, raise, or die by signal so tests drive each failure
class through a real process boundary.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--token",
        action="append",
        default=[],
        help="write this token as its own stdout line (repeatable)",
    )
    parser.add_argument(
        "--prose",
        default="",
        help="append this text to the first --token line (malformed extra prose)",
    )
    parser.add_argument("--stderr", default="", help="write this text to stderr")
    parser.add_argument("--exit", dest="exit_code", type=int, default=0)
    parser.add_argument(
        "--sleep",
        type=float,
        default=0,
        help="sleep this many seconds after writing stdout (for timeout tests)",
    )
    parser.add_argument(
        "--raise",
        dest="do_raise",
        action="store_true",
        help="raise after reading stdin (crash with a traceback, no token)",
    )
    parser.add_argument(
        "--signal",
        dest="sig",
        type=int,
        default=0,
        help="send this signal to self instead of exiting",
    )
    args = parser.parse_args(argv)

    sys.stdin.read()

    if args.do_raise:
        raise RuntimeError("import failure")

    for index, token in enumerate(args.token):
        line = token
        if index == 0 and args.prose:
            line = f"{token} {args.prose}"
        sys.stdout.write(line + "\n")
    if args.token:
        sys.stdout.flush()

    if args.stderr:
        sys.stderr.write(args.stderr)
        if not args.stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()

    if args.sleep:
        time.sleep(args.sleep)

    if args.sig:
        os.kill(os.getpid(), args.sig)

    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
