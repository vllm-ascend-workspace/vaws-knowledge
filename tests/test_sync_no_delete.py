"""Deletion is not a sync operation (docs/federation.md, "Deletion").

Two kinds of proof. Behavioural: every write path in sync/ is driven through
the single write primitive, and that primitive refuses any state in which a
previously present uuid is gone. Structural: no module in sync/ contains a
`del` statement or a `.remove()` / `.clear()` call, and `.pop()` is only used
with a literal key (dropping a report field), never on a list of entries.
"""

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "sync"))

import synctest  # noqa: E402
from synctest import _common, plan_mod, propose_mod, publish_mod, rescan_mod  # noqa: E402


class WritePrimitiveRefusesRemoval(synctest.SyncTestCase):
    def test_document_missing_an_entry_is_refused(self):
        corpus = self.corpus()
        path = self.corpus_dir / "unverified" / "known-failure-signatures.yaml"
        doc = _common.load_yaml(path)
        doc["entries"] = [e for e in doc["entries"] if e["uuid"] != synctest.UUID_UNVERIFIED]
        before = synctest.dir_digest(self.corpus_dir)
        with self.assertRaises(_common.IntegrityError) as ctx:
            _common.write_documents(corpus, {path: doc})
        self.assertIn(synctest.UUID_UNVERIFIED, str(ctx.exception))
        self.assertEqual(before, synctest.dir_digest(self.corpus_dir), "nothing written on refusal")

    def test_empty_document_is_refused(self):
        corpus = self.corpus()
        path = self.corpus_dir / "verified" / "known-failure-signatures.yaml"
        doc = dict(_common.load_yaml(path), entries=[])
        with self.assertRaises(_common.IntegrityError):
            _common.write_documents(corpus, {path: doc})

    def test_moving_an_entry_between_files_is_allowed(self):
        # Not a deletion: the identity is still in the corpus afterwards.
        corpus = self.corpus()
        src = self.corpus_dir / "unverified" / "known-failure-signatures.yaml"
        dst = self.corpus_dir / "unverified" / "moved.yaml"
        src_doc = _common.load_yaml(src)
        moved = [e for e in src_doc["entries"] if e["uuid"] == synctest.UUID_CURRENT]
        src_doc["entries"] = [e for e in src_doc["entries"] if e["uuid"] != synctest.UUID_CURRENT]
        dst_doc = dict(src_doc, entries=moved)
        _common.write_documents(corpus, {src: src_doc, dst: dst_doc})
        self.assertIn(synctest.UUID_CURRENT, self.corpus().index)

    def test_hand_crafted_proposal_dropping_an_entry_is_refused(self):
        corpus = self.corpus()
        e = self.new_entry("omega")
        plan = plan_mod.compute_plan([_common.load_export(self.make_export([e]))], corpus)
        proposal = propose_mod.build_proposal(plan, corpus)
        (doc,) = proposal.changes.values()
        doc["entries"] = [x for x in doc["entries"] if x["uuid"] == e["uuid"]]
        with self.assertRaises(_common.IntegrityError):
            propose_mod.apply_proposal(proposal, corpus)

    def test_export_omitting_entries_does_not_remove_them(self):
        # A fork that stopped exporting an entry is not asking for its removal.
        before = set(self.corpus().index)
        self.propose_apply([self.make_export([self.new_entry("psi")])])
        after = set(self.corpus().index)
        self.assertTrue(before <= after)
        self.assertEqual(len(before) + 1, len(after))


class EveryCommandPreservesIdentities(synctest.SyncTestCase):
    def test_propose_publish_rescan_keep_every_uuid(self):
        before = set(self.corpus().index)
        e = self.entry(synctest.UUID_UNVERIFIED)
        e["rule"]["summary"] = "A reworded summary for the capture warmup failure"
        self.propose_apply([self.make_export([e, self.new_entry("chi")])])
        publish_mod.build_snapshot(self.corpus(), revision="x", generated_at="2026-09-10T00:00:00+00:00")
        rescan_mod.rescan(self.corpus(), "r2")
        after = set(self.corpus().index)
        self.assertTrue(before <= after, before - after)


class StructuralGuard(unittest.TestCase):
    def test_no_module_in_sync_contains_a_removal_construct(self):
        offenders = []
        for path in sorted(synctest.SYNC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Delete):
                    offenders.append(f"{path.name}:{node.lineno} del statement")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                    if name in ("remove", "clear", "unlink", "rmtree"):
                        offenders.append(f"{path.name}:{node.lineno} .{name}()")
                    if name == "pop" and not (
                        node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    ):
                        offenders.append(f"{path.name}:{node.lineno} .pop() without a literal key")
        self.assertEqual([], offenders)

    def test_no_public_function_is_named_for_removal(self):
        names = []
        for mod in (_common, plan_mod, propose_mod, publish_mod, rescan_mod):
            names += [n for n in dir(mod) if callable(getattr(mod, n)) and not n.startswith("_")]
        bad = [n for n in names if any(w in n.lower() for w in ("delete", "remove", "purge", "drop"))]
        self.assertEqual([], bad)


if __name__ == "__main__":
    unittest.main()
