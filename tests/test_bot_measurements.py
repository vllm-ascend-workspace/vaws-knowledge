"""Dedup and conflicts on measurement bodies, against the real corpus.

Two gates read the same pair of entries and must reach opposite conclusions
about it. Dedup asks "are these the same claim, said twice?"; conflicts asks
"are these two claims that cannot both hold?". A measurement pair makes the
distinction sharp, because the comparison is exact rather than a judgement
about prose, and getting it wrong is not a near miss in either direction:

  * a contradiction misfiled as a near duplicate invites a reviewer to merge
    it, which silently discards one of two irreconcilable numbers;
  * a catalogue of distinct subjects misread as a pile of contradictions
    produces ~1900 findings for 63 rows and buries the one that matters.

Both of those were real behaviours of an earlier draft of this change, so both
are asserted here against the corpus that produced them rather than only
against fixtures.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bot import conflicts, corpus, dedup  # noqa: E402
from bot.policy import load_policy  # noqa: E402
from tools.canonical import content_hash  # noqa: E402

CORPUS = REPO / "corpus"


AS_OF = "2026-09-08"


def loaded():
    return corpus.load_paths([CORPUS], root=REPO)


def _restated(original: corpus.EntryRef, entry: dict) -> corpus.EntryRef:
    """The same location, a different entry: a second file saying it again."""
    return corpus.EntryRef(
        path=original.path,
        doc_index=original.doc_index,
        entry_index=original.entry_index + 1,
        kind=original.kind,
        layer=original.layer,
        entry=entry,
    )


class TheMigratedCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load_policy()
        cls.loaded = loaded()
        if not cls.loaded.entries:
            raise unittest.SkipTest("corpus/ carries no entries in this checkout")

    def test_it_loads_without_an_error(self):
        self.assertEqual([], list(self.loaded.errors))
        self.assertTrue(self.loaded.ok)

    def test_every_entry_is_a_measurement_in_the_unverified_zone(self):
        for ref in self.loaded.entries:
            self.assertTrue(ref.is_measurement, ref.slug)
            self.assertEqual("unverified", ref.entry.get("status"), ref.slug)

    def test_no_entry_claims_a_coordinate_it_did_not_establish(self):
        # An unestablished dimension is an unbounded range, never an invented
        # version and never a bare `any` without a basis.
        for ref in self.loaded.entries:
            for dimension, constraint in ref.scope.items():
                if "any" in constraint:
                    self.assertGreaterEqual(
                        len(constraint.get("basis", "")), 12, f"{ref.slug}/{dimension}"
                    )

    def test_a_catalogue_of_distinct_subjects_is_not_a_pile_of_duplicates(self):
        report = dedup.find_duplicates(self.loaded, self.policy)
        self.assertEqual(0, report["counts"]["exact"], report["exact"][:2])
        self.assertEqual(0, report["counts"]["near"], report["near"][:2])

    def test_nor_a_pile_of_contradictions(self):
        # 63 SoCs all declare fp16_dense_matmul_peak/theoretical, all with
        # different values. Different subject, so not a disagreement.
        report = dedup.find_duplicates(self.loaded, self.policy)
        self.assertEqual(
            0, report["counts"]["contradicting"], report["contradicting"][:2]
        )

    def test_and_the_conflicts_gate_agrees_with_dedup_about_that(self):
        report = conflicts.find_conflicts(self.loaded, self.policy, AS_OF)
        self.assertEqual(0, report["counts"]["blocking"], report["conflicts"][:2])
        self.assertEqual(0, report["counts"]["conflicts"], report["conflicts"][:2])

    def test_the_theoretical_and_sustained_entries_share_a_subject(self):
        # Which is the interesting case: two entries about one SoC that are
        # not in conflict, so a gate keyed on the subject alone would be wrong.
        by_subject = {}
        for ref in self.loaded.entries:
            by_subject.setdefault(ref.subject_id, []).append(ref)
        shared = [s for s, refs in by_subject.items() if len(refs) > 1]
        self.assertTrue(shared, "no subject appears twice; this test proves nothing")
        for subject in shared:
            refs = by_subject[subject]
            for i in range(len(refs)):
                for j in range(i + 1, len(refs)):
                    self.assertIsNone(
                        conflicts.measurement_contradiction(refs[i], refs[j]),
                        f"{subject}: {refs[i].slug} vs {refs[j].slug}",
                    )


class ContradictionIsDetectedNotMerged(unittest.TestCase):
    """The pair the brief asks for: one value changed, nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.policy = load_policy()
        base = loaded()
        if not base.entries:
            raise unittest.SkipTest("corpus/ carries no entries in this checkout")
        original = base.entries[0]
        clone = copy.deepcopy(original.entry)
        clone["uuid"] = "0f1e2d3c-4b5a-4968-8776-655443322110"
        clone["slug"] = original.slug + "-restated"
        quantity = clone["measurement"]["quantities"][0]
        cls.quantity_name = quantity["name"]
        quantity["value"] = str(float(quantity["value"]) * 2)
        # A restated claim is a different claim, so it has a different hash.
        # Leaving the copied hash in place would make this an exact duplicate
        # by content_hash and never reach the comparison under test.
        clone["content_hash"] = content_hash(clone)
        cls.loaded = corpus.LoadResult(
            files=list(base.files),
            entries=[original, _restated(original, clone)],
        )

    def test_dedup_declines_to_call_it_a_duplicate(self):
        report = dedup.find_duplicates(self.loaded, self.policy)
        self.assertEqual(0, report["counts"]["exact"])
        self.assertEqual(0, report["counts"]["near"])
        self.assertEqual(1, report["counts"]["contradicting"])
        found = report["contradicting"][0]
        self.assertEqual(0.0, found["score"])
        self.assertIn(self.quantity_name, " ".join(found["disagreeing_quantities"]))
        self.assertIn("Do not merge", found["action"])

    def test_conflicts_blocks_on_it_and_names_the_quantity(self):
        report = conflicts.find_conflicts(self.loaded, self.policy, AS_OF)
        self.assertEqual(1, report["counts"]["blocking"], report)
        record = report["conflicts"][0]
        self.assertEqual(
            [self.quantity_name],
            [d["quantity"] for d in record["signals"]["contradicting_quantities"]],
        )

    def test_the_two_gates_do_not_both_claim_the_pair(self):
        # Exactly one gate offers an action on it, or a reviewer gets told both
        # to merge it and to resolve it.
        near = dedup.find_duplicates(self.loaded, self.policy)["near"]
        blocking = conflicts.find_conflicts(self.loaded, self.policy, AS_OF)["counts"]["blocking"]
        self.assertEqual(0, len(near))
        self.assertEqual(1, blocking)


class UnitsAreNotOptional(unittest.TestCase):
    def test_the_same_number_in_a_different_unit_is_a_contradiction(self):
        # tflops against tops is the failure this catches: 319.34 is either a
        # sensible int8 figure or an impossible fp16 one, depending on a word.
        base = loaded()
        if not base.entries:
            self.skipTest("corpus/ carries no entries in this checkout")
        original = base.entries[0]
        clone = copy.deepcopy(original.entry)
        clone["uuid"] = "11112222-3333-4444-8555-666677778888"
        clone["measurement"]["quantities"][0]["unit"] = "tops"
        clone["content_hash"] = content_hash(clone)
        other = _restated(original, clone)
        differing = conflicts.measurement_contradiction(original, other)
        self.assertIsNotNone(differing)
        self.assertEqual(1, len(differing))
        self.assertNotEqual(differing[0]["a"]["unit"], differing[0]["b"]["unit"])


if __name__ == "__main__":
    unittest.main()
