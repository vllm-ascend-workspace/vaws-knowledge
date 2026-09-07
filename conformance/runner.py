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

  gate command      reads one document on stdin, prints `accept` or `reject`
                    on stdout. If it prints neither, exit status 0 is read as
                    accept and non-zero as reject.

  export command    reads one document on stdin, writes the exported bytes on
                    stdout. Run twice per vector and compared byte for byte.

Usage:

    python3 conformance/runner.py --hash-cmd "python3 tools/canonical.py"
    python3 conformance/runner.py --hash-cmd "..." --payload-cmd "... --payload"
    python3 conformance/runner.py --schema-cmd "python3 tools/validate.py --stdin"
    python3 conformance/runner.py --list

Exit status: 0 if everything run passed, 1 if any vector failed, 2 for a usage
or vector-loading problem. Vector classes with no command are skipped and
reported as skipped; skipping is not passing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_VECTORS = HERE / "vectors"
DEFAULT_GATE_VECTORS = HERE / "gate_vectors"

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ACCEPT_TOKENS = {"accept", "accepted", "ok", "pass", "passed", "valid", "clean"}
REJECT_TOKENS = {"reject", "rejected", "refuse", "refused", "fail", "failed", "invalid"}

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


def run_command(command: str, stdin_bytes: bytes, timeout: float):
    """Run one implementation command. Returns (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(  # noqa: S602 - a command supplied by the caller
            command,
            shell=True,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, b"", f"command timed out after {timeout}s".encode()
    except OSError as exc:
        return None, b"", str(exc).encode()
    return proc.returncode, proc.stdout, proc.stderr


def first_line(data: bytes) -> str:
    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if line:
            return line
    return ""


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
        code, out, err = run_command(hash_cmd, stdin_bytes, timeout)
        expected = str(vector["expected_content_hash"]).strip()
        actual = first_line(out)
        details = []
        if code is None or code != 0:
            details.append(f"      command exited {code}")
            if err.strip():
                details.append(f"      stderr: {first_line(err)}")
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
            pcode, pout, perr = run_command(payload_cmd, stdin_bytes, timeout)
            if pcode == 0:
                details.extend(
                    payload_diff(
                        str(vector["expected_payload"]),
                        pout.decode("utf-8", "replace").rstrip("\n"),
                    )
                )
            else:
                details.append(
                    f"      payload command exited {pcode}: {first_line(perr)}"
                )
        else:
            details.append(
                "      re-run with --payload-cmd to see where the canonical "
                "payload diverges"
            )
        report.record(FAIL, vector["id"], vector["title"], details)


# --- gate vectors ----------------------------------------------------------


def read_verdict(code, out, err):
    """Map a gate command's output to accept / reject, or None if unreadable."""
    token = first_line(out).split()[0].lower().rstrip(":.,") if first_line(out) else ""
    if token in ACCEPT_TOKENS:
        return "accept"
    if token in REJECT_TOKENS:
        return "reject"
    if code is None:
        return None
    return "accept" if code == 0 else "reject"


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
            for index, (code, _out, err) in enumerate((first, second), start=1):
                if code is None or code != 0:
                    details.append(f"      run {index} exited {code}")
                    if err.strip():
                        details.append(f"      stderr: {first_line(err)}")
            if details:
                report.record(FAIL, vector["id"], vector["title"], details)
                continue
            if first[1] == second[1]:
                report.record(PASS, vector["id"], vector["title"])
                continue
            report.record(
                FAIL,
                vector["id"],
                vector["title"],
                [
                    "      two exports of the same document differ",
                    f"      run 1: {len(first[1])} bytes, "
                    f"run 2: {len(second[1])} bytes",
                ]
                + payload_diff(
                    first[1].decode("utf-8", "replace"),
                    second[1].decode("utf-8", "replace"),
                ),
            )
            continue

        code, out, err = run_command(command, stdin_bytes, timeout)
        verdict = read_verdict(code, out, err)
        if verdict is None:
            report.record(
                FAIL,
                vector["id"],
                vector["title"],
                [f"      gate command could not be run: {first_line(err)}"],
            )
            continue
        if verdict == expected:
            report.record(PASS, vector["id"], vector["title"])
            continue
        details = [f"      expected verdict {expected}, got {verdict}"]
        if vector.get("offending"):
            details.append(
                f"      declared offending value: "
                f"{vector['offending'].get('class')} at "
                f"{vector['offending'].get('location')}"
            )
        if vector.get("violation"):
            details.append(
                f"      declared violation: {vector['violation'].get('rule')}"
            )
        if err.strip():
            details.append(f"      stderr: {first_line(err)}")
        report.record(FAIL, vector["id"], vector["title"], details)


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
        "export": args.export_cmd,
    }
    if not args.hash_cmd and not any(gate_commands.values()):
        _die(
            "nothing to run. Give at least one implementation command, e.g.\n"
            "  --hash-cmd \"python3 tools/canonical.py\"\n"
            "  --schema-cmd \"python3 tools/validate.py --stdin\"\n"
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
