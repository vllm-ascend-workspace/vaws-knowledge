"""Run the conformance vectors against an implementation supplied as a command.

The runner imports nothing from the implementation it tests. It talks to it
over a process boundary, because the implementations that have to agree are not
all Python and not all in this repository: `tools/canonical.py` here, a
fallback inside `server/capture.py`, another inside `sync/`, and one client per
contributing fork. A kit that imported a module would only ever be able to test
the first of those.

The contract is therefore a CLI one:

  hash command      reads one entry on stdin, prints `sha256:<64 hex>` on
                    stdout, exits 0. Anything on stderr is ignored.

  payload command   optional. Same input, prints the canonical JSON payload
                    string instead. Only used to produce a readable diff when
                    a hash mismatches - it is not itself a conformance
                    requirement.

  gate command      reads one document on stdin, prints one verdict token
                    (`accept` or `reject`, or an unambiguous documented
                    alias) on stdout, and nothing else. Acceptance requires
                    exit 0 plus an accept token. Rejection requires exit 0
                    or 1 plus a reject token. Exit status alone is never a
                    verdict: a crash, a missing command, a timeout, or
                    malformed stdout is a protocol failure (FAIL), not
                    reject.

                    Three gate classes use this contract: `redaction`,
                    `schema` and `conflicts`. The conflicts gate is fed a
                    document containing more than one entry and must reject
                    when two of them contradict each other: two measurements
                    of the same quantity about the same subject at
                    overlapping coordinates that claim different values.
                    Unlike the other two it is a *cross-entry* gate, which is
                    why the vector document holds several entries and why an
                    implementation cannot answer it one entry at a time.

  export command    reads one document on stdin, writes the exported bytes on
                    stdout. Run twice per vector and compared byte for byte.

Usage:

    python3 conformance/runner.py --hash-cmd "python3 tools/canonical.py"
    python3 conformance/runner.py --hash-cmd "..." --payload-cmd "... --payload"
    python3 conformance/runner.py --schema-cmd "python3 tests/fixtures/conformance/gate_tools_adapter.py schema"
    python3 conformance/runner.py --list

Exit status: 0 if everything run passed, 1 if any vector failed, 2 for a usage
or vector-loading problem. Vector classes with no command are skipped and
reported as skipped; skipping is not passing.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_VECTORS = HERE / "vectors"
DEFAULT_GATE_VECTORS = HERE / "gate_vectors"

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ACCEPT_TOKENS = {"accept", "accepted", "ok", "pass", "passed", "valid", "clean"}
REJECT_TOKENS = {"reject", "rejected", "refuse", "refused", "fail", "failed", "invalid"}

# Surrounding ASCII whitespace around a verdict token is ignored. Unicode
# space (NBSP, etc.) is not, so a token wrapped in it is malformed.
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
STDERR_LIMIT = 240

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _die(message: str) -> "None":
    sys.stderr.write(message.rstrip("\n") + "\n")
    raise SystemExit(2)


def _load_yaml_module():
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        _die(
            "PyYAML is required to read the conformance vectors.\n"
            "Install it with: python3 -m pip install PyYAML\n"
            "(The kit needs the standard library plus PyYAML, nothing else.)"
        )
    return yaml


# --- loading ---------------------------------------------------------------


def load_vector_files(directory: pathlib.Path, required_keys) -> list:
    yaml = _load_yaml_module()
    if not directory.is_dir():
        _die(f"no such vector directory: {directory}")
    vectors = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _die(f"{path.name}: cannot parse: {exc}")
        if not isinstance(data, dict):
            _die(f"{path.name}: vector file must be a mapping")
        missing = [key for key in required_keys if key not in data]
        if missing:
            _die(f"{path.name}: vector is missing {missing}")
        data["_path"] = path
        vectors.append(data)
    if not vectors:
        _die(f"no vectors found in {directory}")
    return vectors


def load_hash_vectors(directory: pathlib.Path) -> list:
    return load_vector_files(
        directory, ("id", "title", "entry", "expected_payload", "expected_content_hash")
    )


def load_gate_vectors(directory: pathlib.Path) -> list:
    return load_vector_files(
        directory, ("id", "title", "gate", "expected_verdict", "document")
    )


# --- talking to the implementation -----------------------------------------


def encode_input(payload, fmt: str) -> bytes:
    if fmt.endswith("-json"):
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    yaml = _load_yaml_module()
    text = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    )
    return text.encode("utf-8")


class CommandResult:
    """Outcome of one implementation command.

    `code` is the process exit status when the process completed, or None
    if it did not (timeout or spawn failure). Partial stdout from a
    timeout is retained so the gate interpreter can prove it is ignored.
    """

    def __init__(self, code, stdout, stderr, failure=None):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.failure = failure  # None | "timeout" | "oserror"


def run_command(command: str, stdin_bytes: bytes, timeout: float):
    """Run one implementation command. Returns a CommandResult."""
    try:
        proc = subprocess.Popen(  # noqa: S602 - a command supplied by the caller
            command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult(None, b"", str(exc).encode(), "oserror")
    try:
        stdout, stderr = proc.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except Exception:
            stdout, stderr = b"", b""
        return CommandResult(None, stdout or b"", stderr or b"", "timeout")
    return CommandResult(proc.returncode, stdout, stderr)


def first_line(data: bytes) -> str:
    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if line:
            return line
    return ""


def bounded_stderr(err: bytes) -> str:
    """One diagnostic line from stderr; never dump a document or traceback."""
    text = err.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # A Python traceback's useful line is the exception, not "Traceback…".
    chosen = lines[-1] if lines[0].startswith("Traceback ") else lines[0]
    if len(chosen) > STDERR_LIMIT:
        return chosen[:STDERR_LIMIT] + "..."
    return chosen


# --- reporting -------------------------------------------------------------


def payload_diff(expected: str, actual: str) -> list:
    """Locate the first divergence between two canonical payload strings."""
    limit = min(len(expected), len(actual))
    offset = limit
    for index in range(limit):
        if expected[index] != actual[index]:
            offset = index
            break
    window_start = max(0, offset - 40)
    lines = [f"      first difference at character {offset}"]
    lines.append(f"      expected ...{expected[window_start:offset + 40]!r}")
    lines.append(f"      actual   ...{actual[window_start:offset + 40]!r}")
    if len(expected) != len(actual):
        lines.append(
            f"      length: expected {len(expected)}, actual {len(actual)}"
        )
    return lines


class Report:
    def __init__(self, verbose: bool = False):
        self.rows = []
        self.verbose = verbose

    def record(self, status: str, vector_id: str, title: str, details=()):
        self.rows.append((status, vector_id, title, list(details)))
        sys.stdout.write(f"{status}  {vector_id}\n")
        if status == FAIL or self.verbose:
            sys.stdout.write(f"      {title}\n")
        for line in details:
            sys.stdout.write(line.rstrip("\n") + "\n")
        sys.stdout.flush()

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row[0] == status)

    def summary(self) -> str:
        return (
            f"{self.count(PASS)} passed, {self.count(FAIL)} failed, "
            f"{self.count(SKIP)} skipped, {len(self.rows)} total"
        )

    @property
    def failed(self) -> bool:
        return self.count(FAIL) > 0


# --- hash vectors ----------------------------------------------------------


def run_hash_vectors(vectors, report, hash_cmd, payload_cmd, fmt, timeout):
    for vector in vectors:
        stdin_bytes = encode_input(vector["entry"], fmt)
        result = run_command(hash_cmd, stdin_bytes, timeout)
        code, out, err = result.code, result.stdout, result.stderr
        expected = str(vector["expected_content_hash"]).strip()
        actual = first_line(out)
        details = []
        if result.failure == "timeout":
            details.append(f"      command timed out after {timeout}s")
            if err.strip():
                details.append(f"      stderr: {bounded_stderr(err)}")
            report.record(FAIL, vector["id"], vector["title"], details)
            continue
        if code is None or code != 0:
            details.append(f"      command exited {code}")
            if err.strip():
                details.append(f"      stderr: {bounded_stderr(err)}")
            report.record(FAIL, vector["id"], vector["title"], details)
            continue
        if not HASH_RE.match(actual):
            details.append(
                "      stdout is not a content_hash: expected sha256:<64 hex>, "
                f"got {actual!r}"
            )
            report.record(FAIL, vector["id"], vector["title"], details)
            continue
        if actual == expected:
            report.record(PASS, vector["id"], vector["title"])
            continue
        details.append(f"      expected {expected}")
        details.append(f"      actual   {actual}")
        if payload_cmd:
            payload = run_command(payload_cmd, stdin_bytes, timeout)
            pcode, pout, perr = payload.code, payload.stdout, payload.stderr
            if pcode == 0:
                details.extend(
                    payload_diff(
                        str(vector["expected_payload"]),
                        pout.decode("utf-8", "replace").rstrip("\n"),
                    )
                )
            else:
                details.append(
                    f"      payload command exited {pcode}: {bounded_stderr(perr)}"
                )
        else:
            details.append(
                "      re-run with --payload-cmd to see where the canonical "
                "payload diverges"
            )
        report.record(FAIL, vector["id"], vector["title"], details)


# --- gate vectors ----------------------------------------------------------


def parse_verdict_token(stdout: bytes):
    """Return (accept|reject|None, error-reason-or-None).

    Surrounding ASCII whitespace and an optional trailing newline are
    ignored. Extra prose, extra lines, or a non-token are malformed.
    """
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None, "malformed stdout (not utf-8)"
    stripped = text.strip(ASCII_WHITESPACE)
    if not stripped:
        return None, "no verdict token"
    if any(char in ASCII_WHITESPACE for char in stripped):
        return None, "malformed stdout (not a single verdict token)"
    token = stripped.lower()
    if token in ACCEPT_TOKENS:
        return "accept", None
    if token in REJECT_TOKENS:
        return "reject", None
    shown = token if len(token) <= 40 else token[:40] + "..."
    return None, f"malformed stdout (unknown token {shown!r})"


def _protocol_details(reason, result):
    details = [f"      gate execution/protocol failure: {reason}"]
    if result.code is not None:
        details.append(f"      exit: {result.code}")
    err_line = bounded_stderr(result.stderr)
    if err_line:
        details.append(f"      stderr: {err_line}")
    return details


def interpret_gate(result: CommandResult):
    """Return (verdict, details). verdict is None on protocol failure."""
    if result.failure == "timeout":
        return None, _protocol_details("timed out before completing", result)
    if result.failure == "oserror":
        return None, _protocol_details("could not be started", result)
    code = result.code
    if code is None:
        return None, _protocol_details("did not complete", result)
    if code < 0:
        return None, _protocol_details(
            f"terminated by signal {-code}", result
        )
    # A shell reports a signaled child as 128+sig rather than a negative
    # wait status. That is still termination, not a semantic verdict.
    if 128 < code <= 159:
        return None, _protocol_details(
            f"terminated by signal {code - 128} (exit {code})", result
        )
    if code in (126, 127):
        return None, _protocol_details(
            f"exit {code} (command not found or not executable)", result
        )
    if code > 1:
        return None, _protocol_details(
            f"exit {code} is not a semantic verdict", result
        )
    token, parse_err = parse_verdict_token(result.stdout)
    if token is None:
        return None, _protocol_details(parse_err, result)
    if token == "accept" and code != 0:
        return None, _protocol_details(
            f"accept token with exit {code} (accept requires exit 0)", result
        )
    return token, []


def read_verdict(code, out, err, *, timed_out=False, os_error=False):
    """Map a completed gate run to accept / reject, or None on failure.

    Exit status is never itself a verdict. A token emitted before
    timeout or termination is ignored: pass timed_out=True, or pass
    code=None, and the result is None even if stdout looks like reject.
    """
    failure = "timeout" if timed_out else ("oserror" if os_error else None)
    verdict, _details = interpret_gate(CommandResult(code, out, err, failure))
    return verdict


def run_gate_vectors(vectors, report, commands, fmt, timeout):
    for vector in vectors:
        gate = vector["gate"]
        command = commands.get(gate)
        expected = vector["expected_verdict"]
        if not command:
            report.record(
                SKIP,
                vector["id"],
                f"[{gate}] {vector['title']} (no --{gate}-cmd given)",
            )
            continue
        stdin_bytes = encode_input(vector["document"], fmt)
        if gate == "export":
            first = run_command(command, stdin_bytes, timeout)
            second = run_command(command, stdin_bytes, timeout)
            details = []
            for index, run in enumerate((first, second), start=1):
                if run.failure == "timeout":
                    details.append(f"      run {index} timed out")
                    if run.stderr.strip():
                        details.append(
                            f"      stderr: {bounded_stderr(run.stderr)}"
                        )
                elif run.code is None or run.code != 0:
                    details.append(f"      run {index} exited {run.code}")
                    if run.stderr.strip():
                        details.append(
                            f"      stderr: {bounded_stderr(run.stderr)}"
                        )
            if details:
                report.record(FAIL, vector["id"], vector["title"], details)
                continue
            if first.stdout == second.stdout:
                report.record(PASS, vector["id"], vector["title"])
                continue
            report.record(
                FAIL,
                vector["id"],
                vector["title"],
                [
                    "      two exports of the same document differ",
                    f"      run 1: {len(first.stdout)} bytes, "
                    f"run 2: {len(second.stdout)} bytes",
                ]
                + payload_diff(
                    first.stdout.decode("utf-8", "replace"),
                    second.stdout.decode("utf-8", "replace"),
                ),
            )
            continue

        result = run_command(command, stdin_bytes, timeout)
        verdict, details = interpret_gate(result)
        if verdict is None:
            report.record(FAIL, vector["id"], vector["title"], details)
            continue
        if verdict == expected:
            report.record(PASS, vector["id"], vector["title"])
            continue
        mismatch = [f"      expected verdict {expected}, got {verdict}"]
        if vector.get("offending"):
            mismatch.append(
                f"      declared offending value: "
                f"{vector['offending'].get('class')} at "
                f"{vector['offending'].get('location')}"
            )
        if vector.get("violation"):
            mismatch.append(
                f"      declared violation: {vector['violation'].get('rule')}"
            )
        if vector.get("contradiction"):
            mismatch.append(
                f"      declared contradiction: {vector['contradiction'].get('rule')}"
            )
        err_line = bounded_stderr(result.stderr)
        if err_line:
            mismatch.append(f"      stderr: {err_line}")
        report.record(FAIL, vector["id"], vector["title"], mismatch)


# --- CLI -------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See conformance/README.md for the full command contract.",
    )
    parser.add_argument("--hash-cmd", help="command that prints an entry's content_hash")
    parser.add_argument(
        "--payload-cmd",
        help="optional command that prints the canonical payload, used for diffs",
    )
    parser.add_argument("--redaction-cmd", help="command that accepts/rejects a document")
    parser.add_argument("--schema-cmd", help="command that accepts/rejects a document")
    parser.add_argument(
        "--conflicts-cmd",
        help="command that accepts/rejects a multi-entry document on contradiction",
    )
    parser.add_argument("--export-cmd", help="command that exports a document to stdout")
    parser.add_argument(
        "--input-format",
        choices=("entry-yaml", "entry-json"),
        default="entry-yaml",
        help="how the entry is written on the hash command's stdin",
    )
    parser.add_argument(
        "--gate-format",
        choices=("document-yaml", "document-json"),
        default="document-yaml",
        help="how the document is written on a gate command's stdin",
    )
    parser.add_argument("--vectors", type=pathlib.Path, default=DEFAULT_VECTORS)
    parser.add_argument("--gate-vectors", type=pathlib.Path, default=DEFAULT_GATE_VECTORS)
    parser.add_argument("--only", help="run only vectors whose id contains this string")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--list", action="store_true", help="print the vector inventory")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def filtered(vectors, only):
    if not only:
        return vectors
    return [vector for vector in vectors if only in vector["id"]]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    hash_vectors = filtered(load_hash_vectors(args.vectors), args.only)
    gate_vectors = filtered(load_gate_vectors(args.gate_vectors), args.only)

    if args.list:
        sys.stdout.write("canonicalization vectors:\n")
        for vector in hash_vectors:
            group = vector.get("invariance_group")
            suffix = f"  [group: {group}]" if group else ""
            sys.stdout.write(f"  {vector['id']}{suffix}\n      {vector['title']}\n")
        sys.stdout.write("gate vectors:\n")
        for vector in gate_vectors:
            sys.stdout.write(
                f"  {vector['id']}  [{vector['gate']} -> "
                f"{vector['expected_verdict']}]\n      {vector['title']}\n"
            )
        return 0

    gate_commands = {
        "redaction": args.redaction_cmd,
        "schema": args.schema_cmd,
        "conflicts": args.conflicts_cmd,
        "export": args.export_cmd,
    }
    if not args.hash_cmd and not any(gate_commands.values()):
        _die(
            "nothing to run. Give at least one implementation command, e.g.\n"
            "  --hash-cmd \"python3 tools/canonical.py\"\n"
            "  --schema-cmd \"python3 tests/fixtures/conformance/"
            "gate_tools_adapter.py schema\"\n"
            "Run --list to see the vector inventory, or --help for the contract."
        )

    report = Report(verbose=args.verbose)

    if args.hash_cmd:
        sys.stdout.write(f"== canonicalization vectors ({len(hash_vectors)})\n")
        run_hash_vectors(
            hash_vectors,
            report,
            args.hash_cmd,
            args.payload_cmd,
            args.input_format,
            args.timeout,
        )
    else:
        sys.stdout.write("== canonicalization vectors: skipped, no --hash-cmd\n")
        for vector in hash_vectors:
            report.record(SKIP, vector["id"], vector["title"])

    sys.stdout.write(f"== gate vectors ({len(gate_vectors)})\n")
    run_gate_vectors(gate_vectors, report, gate_commands, args.gate_format, args.timeout)

    sys.stdout.write(f"\n{report.summary()}\n")
    if report.failed:
        sys.stdout.write(
            "conformance FAILED. A hash mismatch is a divergence, not a bug in "
            "the kit until proven otherwise: check conformance/README.md for the "
            "cases where docs/federation.md is ambiguous.\n"
        )
        return 1
    if report.count(PASS) == 0:
        sys.stdout.write("nothing was actually run.\n")
        return 1
    sys.stdout.write("conformance PASSED for everything that was run.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
