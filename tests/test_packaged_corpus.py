"""The wheel ships the checkout corpus; hashes stay bit-identical."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_tools_support import REPO_ROOT

from vaws_knowledge import canonical, corpus
from vaws_knowledge._common import load_document

EXPECTED_ENTRY_COUNT = 65
_YAML_SUFFIXES = (".yaml", ".yml")


def _checkout_yaml_files() -> list[Path]:
    files: list[Path] = []
    for subset in ("verified", "unverified"):
        directory = REPO_ROOT / "corpus" / subset
        if not directory.is_dir():
            continue
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix in _YAML_SUFFIXES
        )
    return sorted(files)


def _entries_of(doc: object) -> list[object]:
    if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
        return doc["entries"]
    return []


def _build_wheel(out_dir: Path) -> Path:
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "build", "--out-dir", str(out_dir)],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "-w",
                str(out_dir),
                str(REPO_ROOT),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    wheels = sorted(out_dir.glob("vaws_knowledge-*.whl"))
    if not wheels:
        raise AssertionError(f"no wheel produced in {out_dir}")
    return wheels[-1]


class CheckoutCorpus(unittest.TestCase):
    def test_corpus_root_falls_back_to_the_checkout(self):
        root = corpus.corpus_root()
        self.assertTrue(root.is_dir(), root)
        self.assertEqual(root.resolve(), (REPO_ROOT / "corpus").resolve())

    def test_iter_entry_files_matches_checkout_yaml(self):
        found = list(corpus.iter_entry_files())
        expected = _checkout_yaml_files()
        self.assertEqual(found, expected)
        self.assertEqual(len(found), len(expected))


class WheelShipsCorpus(unittest.TestCase):
    def test_installed_wheel_exposes_the_same_entries(self):
        checkout_files = _checkout_yaml_files()
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            wheel = _build_wheel(tmp / "dist")
            sdist_files = list((tmp / "dist").glob("vaws_knowledge-*.tar.gz"))
            if sdist_files:
                listing = subprocess.run(
                    ["tar", "-tzf", str(sdist_files[0])],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                for path in checkout_files:
                    member = path.relative_to(REPO_ROOT).as_posix()
                    self.assertTrue(
                        any(line.endswith(member) for line in listing.splitlines()),
                        f"{member} missing from sdist",
                    )

            venv = tmp / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run(
                [str(python), "-m", "pip", "install", "--quiet", "--no-deps", "--no-index", str(wheel)],
                check=True,
            )
            # This checks wheel contents, not dependency resolution. Reuse only
            # the existing YAML parser to avoid downloading the entire engine
            # stack again; the package under test still comes from the wheel.
            import yaml
            purelib = Path(subprocess.check_output(
                [str(python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
                text=True,
            ).strip())
            shutil.copytree(Path(yaml.__file__).parent, purelib / "yaml")
            script = r"""
import json
from pathlib import Path

from vaws_knowledge import canonical, corpus
from vaws_knowledge._common import load_document

root = corpus.corpus_root()
files = [str(path) for path in corpus.iter_entry_files()]
hashes = []
for path in corpus.iter_entry_files():
    doc = load_document(path)
    entries = doc.get("entries", []) if isinstance(doc, dict) else []
    for entry in entries:
        hashes.append([entry.get("content_hash"), canonical.content_hash(entry)])
print(json.dumps({
    "root": str(root),
    "root_exists": root.is_dir(),
    "file_count": len(files),
    "files": files,
    "hashes": hashes,
    "packaged": "site-packages" in root.as_posix() and "/data/corpus" in root.as_posix(),
}))
"""
            proc = subprocess.run(
                [str(python), "-I", "-c", script],
                cwd=tmp,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["root_exists"], payload["root"])
            self.assertTrue(payload["packaged"], payload["root"])
            self.assertEqual(payload["file_count"], len(checkout_files))
            self.assertEqual(len(payload["hashes"]), EXPECTED_ENTRY_COUNT)
            for stored, recomputed in payload["hashes"]:
                self.assertEqual(recomputed, stored)


class CheckoutHashes(unittest.TestCase):
    def test_every_checkout_entry_hash_matches(self):
        count = 0
        for path in _checkout_yaml_files():
            for entry in _entries_of(load_document(path)):
                self.assertEqual(canonical.content_hash(entry), entry["content_hash"])
                count += 1
        self.assertEqual(count, EXPECTED_ENTRY_COUNT)


if __name__ == "__main__":
    unittest.main()
