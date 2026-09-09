"""CLI exposure of reader coordinates on ``vaws-knowledge query``."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.server.query import (  # noqa: E402
    SCOPE_DIMENSIONS,
    UNKNOWN,
    coordinate_from_manifest,
    discover_run_manifest_path,
    main,
    normalize_coordinate,
    reader_coordinate_from_args,
)


def _config_path(tmp: pathlib.Path) -> pathlib.Path:
    payload = {
        "layers": {
            "shared": {"roots": [str(support.FIXTURES / "shared")]},
            "project": {"roots": [str(support.FIXTURES / "project")]},
            "candidate": {"enabled": False},
        }
    }
    path = tmp / "service.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_cli(*argv: str, env: dict[str, str] | None = None) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict("os.environ", env or {}, clear=False):
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
    return code, json.loads(out.getvalue()), err.getvalue()


class NormalizeUnknownTests(unittest.TestCase):
    def test_unknown_sentinel_is_unsupplied(self) -> None:
        kept, ignored = normalize_coordinate(
            {"soc": UNKNOWN, "cann": "8.2.RC1", "phase_of_moon": "waxing"}
        )
        self.assertEqual({"cann": "8.2.RC1"}, kept)
        self.assertEqual(["phase_of_moon"], ignored)


class ManifestDerivationTests(unittest.TestCase):
    def test_reads_only_declared_dimensions(self) -> None:
        coordinate = coordinate_from_manifest(
            {
                "environment": {"soc": "ExampleSoC-A", "cann": "0.0.EXAMPLE"},
                "model": {"name": "example-model"},
                "topology": {"tensor_parallel_size": 8},
            }
        )
        self.assertEqual("ExampleSoC-A", coordinate["soc"])
        self.assertEqual("0.0.EXAMPLE", coordinate["cann"])
        self.assertEqual("example-model", coordinate["model"])
        self.assertEqual("tp8", coordinate["topology"])
        self.assertNotIn("torch", coordinate)
        self.assertNotIn("component", coordinate)

    def test_unknown_in_manifest_is_skipped(self) -> None:
        coordinate = coordinate_from_manifest({"environment": {"soc": "unknown", "torch": "2.7.1"}})
        self.assertNotIn("soc", coordinate)
        self.assertEqual("2.7.1", coordinate["torch"])


class DiscoverManifestTests(unittest.TestCase):
    def test_env_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            named = root / "explicit.json"
            named.write_text("{}", encoding="utf-8")
            env_path = root / "from-env.json"
            env_path.write_text("{}", encoding="utf-8")
            cwd_file = root / "run-manifest.json"
            cwd_file.write_text("{}", encoding="utf-8")
            self.assertEqual(
                named,
                discover_run_manifest_path(str(named), cwd=root, env={}),
            )
            self.assertEqual(
                env_path,
                discover_run_manifest_path(
                    None, cwd=root, env={"VAWS_RUN_MANIFEST": str(env_path)}
                ),
            )
            self.assertEqual(
                cwd_file,
                discover_run_manifest_path(None, cwd=root, env={}),
            )

    def test_missing_explicit_path_is_an_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            discover_run_manifest_path("/no/such/manifest.json", cwd=pathlib.Path("/"), env={})
        self.assertIn("does not exist", str(ctx.exception))


class CliCoordinateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = _config_path(pathlib.Path(self.tmp.name))

    def test_soc_mismatch_withholds_the_other_entry(self) -> None:
        code, payload, _ = _run_cli(
            "--config",
            str(self.config),
            "--soc",
            "ExampleSoC-A",
        )
        self.assertEqual(0, code)
        uuids = [item["uuid"] for item in payload["results"]]
        self.assertIn(support.SHARED_SOC_A, uuids)
        self.assertNotIn(support.SHARED_SOC_B, uuids)
        self.assertEqual("ExampleSoC-A", payload["reader_coordinate"]["supplied"]["soc"])

    def test_reader_coordinate_is_populated(self) -> None:
        code, payload, _ = _run_cli(
            "--config",
            str(self.config),
            "--soc",
            "ExampleSoC-A",
            "--topology",
            "tp8",
        )
        self.assertEqual(0, code)
        supplied = payload["reader_coordinate"]["supplied"]
        self.assertEqual({"soc": "ExampleSoC-A", "topology": "tp8"}, supplied)
        self.assertIn("cann", payload["reader_coordinate"]["unsupplied_expected_dimensions"])

    def test_manifest_fills_dimensions_without_flags(self) -> None:
        manifest = {
            "environment": {"soc": "ExampleSoC-A", "cann": "0.0.EXAMPLE"},
            "model": {},
            "topology": {},
        }
        args = mock.Mock(
            run_manifest="/tmp/does-not-matter.json",
            reader_coordinate=None,
            **{f"coord_{name}": None for name in SCOPE_DIMENSIONS},
        )
        with mock.patch(
            "vaws_knowledge.server.query.discover_run_manifest_path",
            return_value=pathlib.Path("/tmp/does-not-matter.json"),
        ), mock.patch(
            "vaws_knowledge.server.query.load_run_manifest",
            return_value=manifest,
        ):
            coordinate = reader_coordinate_from_args(args, cwd=pathlib.Path(self.tmp.name), env={})
        self.assertEqual({"soc": "ExampleSoC-A", "cann": "0.0.EXAMPLE"}, coordinate)

    def test_missing_coordinator_is_a_hard_failure(self) -> None:
        path = pathlib.Path(self.tmp.name) / "run-manifest.json"
        path.write_text("{}", encoding="utf-8")
        import builtins

        real_import = builtins.__import__

        def blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("vaws_coordinator"):
                raise ImportError("No module named 'vaws_coordinator'")
            return real_import(name, globals, locals, fromlist, level)

        from vaws_knowledge.server.query import load_run_manifest

        with mock.patch("builtins.__import__", side_effect=blocked):
            with self.assertRaises(SystemExit) as ctx:
                load_run_manifest(path)
        self.assertIn("vaws_coordinator.run_manifest is not importable", str(ctx.exception))
        self.assertIn("uv sync", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
