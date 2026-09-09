"""Mount configuration and labelling for the three layers.

The property under test is that absence is a reported state, not an error and
not a silent empty set. A caller that cannot tell "this layer was not mounted"
from "this layer had nothing to say" will read a gap as a fact.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.corpus import corpus_root  # noqa: E402
from vaws_knowledge.server.layers import (  # noqa: E402
    DEFAULT_STATUSES,
    LAYERS,
    SOURCE_REPO,
    ConfigError,
    default_shared_roots,
    load_config,
    load_entries,
    resolve_shared_from_corpus,
    shared_source,
)


class LayerMounting(unittest.TestCase):
    def test_all_three_layers_mount_and_are_labelled(self):
        config = support.build_config()
        self.assertEqual(["shared", "project", "candidate"], config.available_layers())
        self.assertEqual({}, config.absent_layers())
        self.assertFalse(config.describe()["degraded"])

    def test_shared_is_read_only_even_if_configuration_asks_otherwise(self):
        # docs/federation.md: a fork never writes into verified/. That is not
        # negotiable through configuration.
        config = load_config(
            {"layers": {"shared": {"roots": ["shared"], "read_only": False}}},
            env={},
            base_dir=support.FIXTURES,
        )
        self.assertTrue(config.mount("shared").read_only)
        self.assertFalse(config.mount("candidate").read_only)

    def test_unconfigured_project_layer_is_absent_not_an_error(self):
        config = load_config({"layers": {"shared": {"roots": ["shared"]}}}, env={}, base_dir=support.FIXTURES)
        self.assertEqual("shared", config.available_layers()[0])
        self.assertIn("project", config.absent_layers())
        self.assertIn("not configured", config.absent_layers()["project"])

    def test_missing_path_is_absent_with_the_path_in_the_reason(self):
        config = support.build_config(shared="missing")
        self.assertNotIn("shared", config.available_layers())
        self.assertIn("does not exist", config.absent_layers()["shared"])
        self.assertTrue(config.mount("shared").configured)

    def test_missing_candidate_root_is_an_empty_present_layer(self):
        config = support.build_config(candidate="missing")
        mount = config.mount("candidate")
        self.assertTrue(mount.present)
        self.assertTrue(mount.configured)
        self.assertNotIn("candidate", config.absent_layers())
        self.assertFalse(config.degraded(["candidate"]))

    def test_unreadable_candidate_root_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = pathlib.Path(tmp) / "not-a-directory"
            blocker.write_text("nope\n", encoding="utf-8")
            config = support.build_config(candidate=blocker)
            self.assertFalse(config.mount("candidate").present)
            self.assertIn("not a directory", config.absent_layers()["candidate"])

    def test_no_shared_cache_still_yields_the_local_layers(self):
        config = support.build_config(shared="missing")
        report = load_entries(config, ["shared", "project", "candidate"])
        self.assertEqual([], report.errors)
        self.assertTrue(report.entries)
        self.assertEqual({"project", "candidate"}, {e.layer for e in report.entries})

    def test_disabled_layer_reports_why(self):
        config = support.build_config(candidate=False)
        self.assertEqual("disabled in configuration", config.absent_layers()["candidate"])

    def test_service_config_describe_lists_every_layer(self):
        described = support.build_config().describe()
        self.assertEqual(set(LAYERS), set(described["layers"]))
        self.assertEqual(list(DEFAULT_STATUSES), described["policy"]["default_statuses"])
        self.assertEqual(SOURCE_REPO, described["source_repo"])
        self.assertIn("source_ref", described)


class EnvironmentOverrides(unittest.TestCase):
    def test_env_roots_override_the_config_file(self):
        config = load_config(
            {"layers": {"project": {"roots": ["no-such-directory"]}}},
            env={"VAWS_KNOWLEDGE_PROJECT_ROOTS": str(support.FIXTURES / "project")},
            base_dir=support.FIXTURES,
        )
        self.assertTrue(config.mount("project").present)

    def test_empty_env_value_disables_a_layer(self):
        config = support.build_config(env={"VAWS_KNOWLEDGE_SHARED_ROOTS": ""})
        self.assertNotIn("shared", config.available_layers())
        self.assertIn("disabled by VAWS_KNOWLEDGE_SHARED_ROOTS", config.absent_layers()["shared"])

    def test_layer_allowlist_env_narrows_the_mount_set(self):
        config = support.build_config(env={"VAWS_KNOWLEDGE_LAYERS": "shared"})
        self.assertEqual(["shared"], config.available_layers())
        self.assertIn("VAWS_KNOWLEDGE_LAYERS", config.absent_layers()["project"])

    def test_identity_and_policy_come_from_the_environment(self):
        config = support.build_config(
            env={
                "VAWS_KNOWLEDGE_CONTRIBUTOR": "example-handle",
                "VAWS_KNOWLEDGE_ORIGIN_REPO": "example-org/example-repo",
                "VAWS_KNOWLEDGE_REDACTION_PROFILE": "r7",
                "VAWS_KNOWLEDGE_STALE_AFTER_DAYS": "30",
            }
        )
        self.assertEqual("example-handle", config.identity["contributor"])
        self.assertEqual("example-org/example-repo", config.identity["origin_repo"])
        self.assertEqual("r7", config.identity["redaction_profile"])
        self.assertEqual(30, config.stale_after_days)

    def test_unparsable_staleness_horizon_warns_and_keeps_the_default(self):
        config = support.build_config(env={"VAWS_KNOWLEDGE_STALE_AFTER_DAYS": "soon"})
        self.assertEqual(180, config.stale_after_days)
        self.assertTrue(any("not an integer" in w for w in config.warnings))


class ConfigFiles(unittest.TestCase):
    def test_relative_roots_resolve_against_the_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vaws-knowledge.json"
            path.write_text(
                json.dumps(
                    {
                        "layers": {
                            "shared": {"roots": [str(support.FIXTURES / "shared")]},
                            "project": {"roots": ["project-knowledge"]},
                        }
                    }
                )
            )
            (pathlib.Path(tmp) / "project-knowledge").mkdir()
            config = load_config(path=path, env={})
            self.assertTrue(config.mount("shared").present)
            self.assertTrue(config.mount("project").present)
            self.assertEqual(path, config.config_path)

    def test_missing_config_file_warns_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(path=pathlib.Path(tmp) / "absent.json", env={})
            self.assertIsNone(config.config_path)
            self.assertTrue(any("config file not found" in w for w in config.warnings))

    def test_malformed_config_file_raises_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "vaws-knowledge.json"
            path.write_text("{not json")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path=path, env={})
            self.assertIn("cannot read config", str(ctx.exception))

    def test_unknown_layer_in_config_is_a_warning_not_a_failure(self):
        config = load_config(
            {"layers": {"shared": {"roots": ["shared"]}, "archive": {"roots": ["shared"]}}},
            env={},
            base_dir=support.FIXTURES,
        )
        self.assertTrue(any("unknown layer" in w for w in config.warnings))
        self.assertTrue(config.mount("shared").present)


class PackagedCorpusDefault(unittest.TestCase):
    def test_default_shared_roots_are_both_packaged_subsets(self):
        roots = default_shared_roots({})
        self.assertEqual(
            {name.name for name in roots},
            {"verified", "unverified"},
        )
        self.assertEqual(
            tuple(p.resolve() for p in roots),
            tuple(p.resolve() for p in resolve_shared_from_corpus(corpus_root())),
        )

    def test_corpus_env_still_resolves_through_resolve_shared_from_corpus(self):
        roots = default_shared_roots({"VAWS_KNOWLEDGE_CORPUS": str(support.REPO)})
        self.assertEqual(
            tuple(p.resolve() for p in roots),
            tuple(p.resolve() for p in resolve_shared_from_corpus(support.REPO)),
        )
        self.assertEqual({p.name for p in roots}, {"verified", "unverified"})

    def test_load_entries_reads_the_sixty_four_packaged_entries(self):
        config = load_config({}, env={"VAWS_KNOWLEDGE_CANDIDATE_ROOT": ""})
        report = load_entries(config, ["shared"])
        self.assertEqual([], report.errors)
        self.assertEqual(64, len(report.entries))
        self.assertEqual({"shared"}, {e.layer for e in report.entries})

    def test_shared_source_names_the_commons_repo(self):
        source = shared_source()
        self.assertEqual(SOURCE_REPO, source["source_repo"])
        self.assertIn("source_ref", source)


class DocumentLoading(unittest.TestCase):
    def test_every_entry_carries_its_layer_and_relative_source(self):
        config = support.build_config()
        report = load_entries(config, ["shared", "project", "candidate"])
        self.assertEqual(9, len(report.entries))
        for loaded in report.entries:
            self.assertIn(loaded.layer, LAYERS)
            self.assertEqual("known-failure-signatures", loaded.kind)
            self.assertFalse(pathlib.Path(loaded.source).is_absolute())
        candidate = [e for e in report.entries if e.layer == "candidate"]
        self.assertEqual(["unverified"], [e.document_layer for e in candidate])

    def test_a_malformed_document_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "broken.yaml").write_text("entries: [ unterminated\n")
            (root / "not-a-document.yaml").write_text("just a string\n")
            (root / "no-entries.json").write_text(json.dumps({"kind": "x"}))
            (root / "usable.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "known-failure-signatures",
                        "layer": "unverified",
                        "updated_at": "2026-09-07",
                        "entries": [{"uuid": support.CANDIDATE_ONLY, "slug": "x"}],
                    }
                )
            )
            config = support.build_config(shared=False, project=False, candidate=root)
            report = load_entries(config, ["candidate"])
            self.assertEqual(1, len(report.entries))
            self.assertEqual(3, len(report.errors))
            self.assertEqual({"candidate"}, {err["layer"] for err in report.errors})
            for err in report.errors:
                self.assertFalse(pathlib.Path(err["file"]).is_absolute())

    def test_entry_without_uuid_is_skipped_with_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "doc.json").write_text(
                json.dumps({"kind": "k", "layer": "unverified", "entries": [{"slug": "no-uuid"}]})
            )
            config = support.build_config(shared=False, project=False, candidate=root)
            report = load_entries(config, ["candidate"])
            self.assertEqual([], report.entries)
            self.assertIn("no uuid", report.errors[0]["error"])


if __name__ == "__main__":
    unittest.main()
