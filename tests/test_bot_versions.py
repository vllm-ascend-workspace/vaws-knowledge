"""The normative ordering table from docs/version-ordering.md, as tests.

This file exists because the ordering was not specified at first, three
implementations each invented one, and two of them disagreed about whether
`8.0.RC10` is newer than `8.0.RC2`. The specification settled it; these cases
make the settlement enforceable, so a future edit that quietly reintroduces a
convenient rule fails here instead of in somebody's query results.

Every row of the specification's worked-examples table appears below. When that
table changes, this file changes with it, in the same commit — a specification
whose examples nothing checks drifts from its implementations silently, which is
exactly the state that produced the original disagreement.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
from vaws_knowledge.bot import versions  # noqa: E402

LESS, EQUAL, GREATER, UNDECIDABLE = -1, 0, 1, None


class WorkedExamples(unittest.TestCase):
    """docs/version-ordering.md, "Worked examples"."""

    CASES = [
        # (a, b, dimension, expected, why)
        ("8.0.RC2", "8.0.RC3", "cann", LESS, "runs ['rc',2] vs ['rc',3]"),
        ("8.0.RC2", "8.0.RC10", "cann", LESS, "digit runs compare as integers, not text"),
        ("8.0.RC2", "8.1.RC1", "cann", LESS, "decided at the second segment, 0 < 1"),
        ("8.0.RC2", "8.0", "cann", UNDECIDABLE, "extra segment is not all digits"),
        ("24.1.rc3", "24.1.rc3", "driver", EQUAL, "identical"),
        ("2.5.1", "2.5.1.post1", "torch", LESS, "PEP 440 post-release"),
        ("0.11.0rc1", "0.11.0", "vllm", LESS, "PEP 440 pre-release"),
        (
            "2.5.1+gitc4b1234",
            "2.5.1",
            "torch_npu",
            GREATER,
            "PEP 440 orders local labels after the base version",
        ),
        ("0.0.0+example", "0.1.0", "vllm", LESS, "PEP 440 parses both"),
        ("0.0.EXAMPLE", "8.0.RC2", "cann", LESS, "decided at the first segment, 0 < 8"),
        (
            "0.0.EXAMPLE",
            "0.0.RC1",
            "cann",
            LESS,
            "both third segments start with a letter run, 'example' < 'rc'",
        ),
        ("0.0.EXAMPLE", "0.0.1", "cann", UNDECIDABLE, "letter run against digit run"),
        ("8.0.2", "8.0.RC2", "cann", UNDECIDABLE, "same, in the other direction"),
    ]

    @staticmethod
    def _skip_reason(dimension: str) -> str | None:
        """PEP 440 rows need `packaging`; the rule is to report undecidable.

        Without it a conforming implementation must return undecidable rather
        than substituting another ordering, so those rows genuinely cannot be
        asserted. Skipping with a stated reason is the honest outcome; failing
        would report a specification violation that is not there, and asserting
        undecidable instead would pin the degraded behaviour as if it were the
        rule.
        """
        if versions.scheme_for(dimension) != "pep440":
            return None
        try:
            import packaging.version  # noqa: F401, PLC0415
        except ImportError:
            return "packaging is not installed; PEP 440 rows correctly degrade to undecidable"
        return None

    def test_every_worked_example(self):
        for a, b, dimension, expected, why in self.CASES:
            with self.subTest(a=a, b=b, dimension=dimension, why=why):
                reason = self._skip_reason(dimension)
                if reason:
                    self.skipTest(reason)
                self.assertEqual(
                    expected,
                    versions.compare(a, b, dimension=dimension),
                    f"{a} vs {b} on {dimension}: {why}",
                )

    def test_comparison_is_antisymmetric(self):
        # A rule that says a < b and also b < a is worse than one that says
        # undecidable, because it produces different results depending on
        # argument order.
        for a, b, dimension, expected, _why in self.CASES:
            with self.subTest(a=a, b=b, dimension=dimension):
                reason = self._skip_reason(dimension)
                if reason:
                    self.skipTest(reason)
                reverse = versions.compare(b, a, dimension=dimension)
                if expected is UNDECIDABLE:
                    self.assertIsNone(reverse)
                else:
                    self.assertEqual(-expected, reverse)


class UndecidabilityIsPerPair(unittest.TestCase):
    """The error an earlier revision of the specification made.

    It claimed `0.0.EXAMPLE` was undecidable against "any real CANN version".
    A value that cannot be ordered against one version can be perfectly
    orderable against another, so an implementation that short-circuits on
    "this string looks unorderable" disagrees with one that follows the steps.
    """

    def test_the_same_value_is_decidable_against_one_and_not_another(self):
        # Decided at the first segment.
        self.assertEqual(LESS, versions.compare("0.0.EXAMPLE", "8.0.RC2", dimension="cann"))
        # Decided by two letter runs, which is where the second wrong reading of
        # the specification expected an undecidable result.
        self.assertEqual(LESS, versions.compare("0.0.EXAMPLE", "0.0.RC1", dimension="cann"))
        # Genuinely undecidable: a letter run where the other side has digits.
        self.assertIsNone(versions.compare("0.0.EXAMPLE", "0.0.1", dimension="cann"))


class ExactOnlyDimensions(unittest.TestCase):
    """A range on an exact-match-only dimension is malformed, not ambiguous."""

    def test_range_on_exact_dimension_is_a_contract_violation(self):
        for dimension in ("soc", "python_abi", "model", "topology", "execution_mode", "component"):
            with self.subTest(dimension=dimension):
                with self.assertRaises(versions.RangeNotAllowed):
                    versions.compare("a", "b", dimension=dimension)

    def test_unknown_dimension_fails_closed_as_exact(self):
        # The schema can grow a dimension before the scheme table does, and
        # inventing an ordering for a dimension nobody has considered is the
        # guessing this module refuses to do.
        self.assertEqual("exact", versions.scheme_for("some_future_dimension"))
        with self.assertRaises(versions.RangeNotAllowed):
            versions.compare("1", "2", dimension="some_future_dimension")


class Bounds(unittest.TestCase):
    def test_null_bound_is_always_satisfied(self):
        self.assertTrue(versions.within("8.0.RC2", None, None, dimension="cann"))
        self.assertTrue(versions.within("8.0.RC2", "8.0.RC1", None, dimension="cann"))
        self.assertTrue(versions.within("8.0.RC2", None, "8.0.RC3", dimension="cann"))

    def test_range_is_inclusive(self):
        self.assertTrue(versions.within("8.0.RC2", "8.0.RC2", "8.0.RC2", dimension="cann"))

    def test_value_outside_the_range_is_a_miss(self):
        self.assertFalse(versions.within("8.0.RC4", "8.0.RC1", "8.0.RC3", dimension="cann"))

    def test_one_undecidable_comparison_makes_containment_undecidable(self):
        # Not a miss. Reporting a miss would read as "this entry does not apply
        # to you", which is a claim nobody established.
        self.assertIsNone(versions.within("8.0", "8.0.RC1", "8.0.RC3", dimension="cann"))


class NaturalSegmentEdges(unittest.TestCase):
    def test_build_metadata_is_dropped_for_the_natural_scheme(self):
        self.assertEqual(EQUAL, versions.compare("8.0.RC2+local", "8.0.RC2", dimension="cann"))

    def test_separators_are_equivalent(self):
        self.assertEqual(EQUAL, versions.compare("24.1.rc3", "24-1_rc3", dimension="driver"))

    def test_empty_input_is_undecidable_against_everything(self):
        self.assertIsNone(versions.compare("", "8.0.RC2", dimension="cann"))
        self.assertIsNone(versions.compare("   ", "8.0.RC2", dimension="cann"))

    def test_trailing_numeric_segments_order_above_the_shorter_value(self):
        self.assertEqual(LESS, versions.compare("8.0", "8.0.1", dimension="cann"))


class Pep440Degradation(unittest.TestCase):
    def test_unparseable_pep440_input_is_undecidable(self):
        self.assertIsNone(versions.compare("not-a-version", "2.5.1", dimension="torch"))

    def test_missing_packaging_reports_undecidable_rather_than_substituting(self):
        # The important half of the rule. Falling back to the natural scheme
        # would keep producing plausible answers under a different ordering,
        # which is invisible divergence rather than a visible gap.
        real_import = __import__

        def blocked(name, *args, **kwargs):
            if name.startswith("packaging"):
                raise ImportError("packaging is unavailable in this test")
            return real_import(name, *args, **kwargs)

        import builtins

        original = builtins.__import__
        builtins.__import__ = blocked
        try:
            self.assertIsNone(versions.compare("2.5.1", "2.5.1.post1", dimension="torch"))
            # The natural scheme is unaffected: it has no such dependency.
            self.assertEqual(LESS, versions.compare("8.0.RC2", "8.0.RC3", dimension="cann"))
        finally:
            builtins.__import__ = original


if __name__ == "__main__":
    unittest.main()
