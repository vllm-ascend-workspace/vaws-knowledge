"""Shared helpers for tests/test_sync_*.py.

Kept next to the fixtures rather than in tests/ so unittest discovery does not
pick it up as a test module. Everything here is offline: it copies the fixture
corpus into a temporary directory, builds exports programmatically from the
fixture entries and recomputes `content_hash` with the same canonicalization
the sync code uses.
"""

from __future__ import annotations

import copy
import hashlib
import pathlib
import shutil
import sys
import tempfile
import unittest

FIXTURES = pathlib.Path(__file__).resolve().parent
REPO = FIXTURES.parent.parent.parent
CORPUS_BASE = FIXTURES / "corpus_base"
TOOLS_PASS = FIXTURES / "tools_pass"
TOOLS_MARKER = FIXTURES / "tools_marker"
TOOLS_MISSING = FIXTURES / "tools_missing"
EXPORT_FIXTURE = FIXTURES / "exports" / "fork-export.yaml"

KIND = "known-failure-signatures"
UUID_VERIFIED = "1416a279-1215-4adf-a978-82b40a3be0bc"  # examples/valid-entry.yaml
UUID_MARKER = "3b9d5c2e-7f41-4a8b-9c2d-0e1f2a3b4c5d"  # verified, carries QUARANTINE-ME
UUID_UNVERIFIED = "5d2f8e1a-3c4b-4d6e-8f9a-1b2c3d4e5f6a"  # unverified, r1, has verification
UUID_CURRENT = "7e4a1b9c-2d3e-4f50-a1b2-c3d4e5f6a7b8"  # unverified, already r2
UUID_EXPORT_NEW = "9c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"  # only in exports/fork-export.yaml

try:
    import yaml  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML is required for the sync suite: {exc}") from exc

import vaws_knowledge.sync as _sync_pkg
from vaws_knowledge.sync import _common
from vaws_knowledge.sync import plan as plan_mod
from vaws_knowledge.sync import propose as propose_mod
from vaws_knowledge.sync import publish as publish_mod
from vaws_knowledge.sync import rescan as rescan_mod

SYNC_DIR = pathlib.Path(_sync_pkg.__file__).resolve().parent


def fresh_uuid(seed: str) -> str:
    """A deterministic v4-shaped uuid for tests."""
    h = hashlib.sha256(seed.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def dir_digest(root: pathlib.Path) -> dict[str, str]:
    out = {}
    for p in sorted(pathlib.Path(root).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class SyncTestCase(unittest.TestCase):
    """A temporary working copy of the fixture corpus per test."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="vaws-sync-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.corpus_dir = self.tmp / "corpus"
        shutil.copytree(CORPUS_BASE, self.corpus_dir)

    # -- corpus access ----------------------------------------------------

    def corpus(self) -> _common.Corpus:
        return _common.load_corpus(self.corpus_dir)

    def entry(self, uid: str) -> dict:
        return copy.deepcopy(self.corpus().index[uid].entry)

    # -- export construction ----------------------------------------------

    def make_export(self, entries: list[dict], *, kind: str = KIND, origin: str = "fork-a/vllm-ascend-workspace",
                    name: str = "export.yaml", rehash: bool = True) -> pathlib.Path:
        entries = copy.deepcopy(entries)
        for e in entries:
            e.setdefault("provenance", {})["origin_repo"] = origin
            if rehash:
                e["content_hash"] = _common.content_hash(e)
        doc = {"schema_version": 2, "kind": kind, "layer": "unverified", "updated_at": "2026-09-08",
               "entries": entries}
        path = self.tmp / name
        path.write_text(_common.dump_yaml(doc), encoding="utf-8")
        return path

    def new_entry(self, seed: str, **rule_overrides) -> dict:
        e = self.entry(UUID_UNVERIFIED)
        e["uuid"] = fresh_uuid(seed)
        e["slug"] = f"test-{seed}"
        e["rule"].update(
            {
                "summary": f"Synthetic {seed}: the launcher exits before any rank starts",
                "symptom": f"Synthetic {seed}: exit code one with no device log lines at all",
                "root_cause": f"Synthetic {seed}: a required environment variable is empty",
                "resolution": f"Synthetic {seed}: set the variable in the launch wrapper",
                "fingerprints": [f"synthetic {seed} launcher exit", f"synthetic {seed} empty env"],
            }
        )
        e["rule"].update(rule_overrides)
        e["content_hash"] = _common.content_hash(e)
        return e

    # -- running the planner / proposer -----------------------------------

    def plan(self, export_paths: list[pathlib.Path], *, day: str = "2026-09-09") -> plan_mod.Plan:
        exports = [_common.load_export(p) for p in export_paths]
        return plan_mod.compute_plan(exports, self.corpus(), day=day)

    def propose_apply(self, export_paths: list[pathlib.Path], *, day: str = "2026-09-09", **kwargs):
        corpus = self.corpus()
        exports = [_common.load_export(p) for p in export_paths]
        plan = plan_mod.compute_plan(exports, corpus, day=day)
        proposal = propose_mod.build_proposal(plan, corpus, **kwargs)
        written = propose_mod.apply_proposal(proposal, corpus)
        return plan, proposal, written
