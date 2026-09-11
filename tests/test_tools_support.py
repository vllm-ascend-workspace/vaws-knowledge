"""Shared helpers for the tools/ test-suite, plus environment sanity tests.

Sensitive-looking strings (addresses, user paths, host names) needed by the
redaction tests are assembled here from parts at run time so that no such
value is ever committed to this public repository.
"""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
import yaml  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "tools"
EXAMPLE_ENTRY = REPO_ROOT / "examples" / "valid-entry.yaml"

#: The cross-implementation anchor from examples/valid-entry.yaml.
ANCHOR_HASH = "sha256:32d1e6611f47083c885205b4f4ef398ea238c0e7eaa60a3e3d961c49ce5b166a"


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def valid_document() -> dict[str, Any]:
    """A deep copy of the valid fixture document."""
    return copy.deepcopy(load_yaml(FIXTURES / "valid" / "known-failure-signatures.yaml"))


def write_yaml(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def run_tool(name: str, *args: str, python: str | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m vaws_knowledge <name> ARGS`` from the repo root."""
    cmd = [python or sys.executable, "-m", "vaws_knowledge", name, *args]
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Synthetic leak values, built from parts so the literal never appears in git.
# --------------------------------------------------------------------------- #

def synthetic_ipv4(last: int = 153) -> str:
    return ".".join(str(o) for o in (192, 168, 13, last))


def synthetic_ipv4_range() -> str:
    return synthetic_ipv4(153) + "-156"


def calibration_sentence() -> str:
    """The class of leak the v1 corpus contained: a free-text applicability
    field that names the machines a fact was verified on."""
    return (
        "Verified 2026-09-02 on workspace A3 containers (vllm-ascend images), "
        f"TP2/TP4/TP8/TP16 services on {synthetic_ipv4_range()}."
    )


def synthetic_user_path() -> str:
    return "/" + "/".join(("home", "exampleuser", "work", "vllm-ascend-workspace"))


def synthetic_mac_path() -> str:
    return "/" + "/".join(("Users", "exampleuser", "code"))


def synthetic_hostname() -> str:
    return "-".join(("npu", "node", "07")) + "." + ".".join(("cluster", "internal"))


def synthetic_email() -> str:
    return "example.person" + "@" + "corp-mail" + "." + "org"


def synthetic_mac() -> str:
    return ":".join(("02", "42", "ac", "11", "00", "07"))


def synthetic_ipv6() -> str:
    return "fd12" + ":" + "3456" + "::" + "7"


def synthetic_container_command() -> str:
    return "docker exec -it " + "vaws-" + "sess-" + "a3-153" + " bash"


def synthetic_credential() -> str:
    return "api_key=" + "AbC" + "12345XyZ" + "98765" + "QwErTy" + "0000000000000"


def versionlike(last: str = "1") -> str:
    """A benign 4-part version string that the IPv4 rule will (correctly) flag."""
    return ".".join(("7", "0", "0", last))


def pem_header() -> str:
    return "-----BEGIN " + "RSA PRIVATE KEY-----"


def synthetic_ticket() -> str:
    return "INTERNAL-" + "48213"


def synthetic_employee_id() -> str:
    return "w" + "00123456"


class TempDir:
    """Context manager yielding a fresh temporary directory as a Path."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="vaws-tools-test-")
        return Path(self._tmp.name)

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class EnvironmentTests(unittest.TestCase):
    def test_fixture_tree_present(self):
        self.assertTrue((FIXTURES / "valid" / "known-failure-signatures.yaml").is_file())
        self.assertTrue(EXAMPLE_ENTRY.is_file())

    def test_missing_jsonschema_gives_actionable_message_not_traceback(self):
        # Simulate an interpreter without jsonschema by poisoning sys.modules
        # before the tool imports it.
        code = (
            "import sys\n"
            "sys.modules['jsonschema'] = None\n"
            "from vaws_knowledge.cli import main\n"
            f"raise SystemExit(main(['validate', {str(EXAMPLE_ENTRY)!r}]))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("jsonschema", proc.stderr)
        self.assertIn("pip install -e .", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_no_tracked_fixture_contains_a_redaction_hit(self):
        # Belt and braces: the fixture tree itself must be clean under the
        # built-in ruleset, otherwise a test fixture is the leak.
        proc = run_tool("redact", "--check", str(FIXTURES), str(REPO_ROOT / "examples"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
