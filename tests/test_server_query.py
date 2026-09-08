"""Retrieval, labelling and coordinate matching.

The load-bearing assertions here are the negative ones: an entry established
on another SoC must not come back looking as though it applied, and an entry
that matched only because it *claimed* independence must say so. A retrieval
layer that quietly widens applicability turns a reviewed corpus back into a
wiki.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from vaws_knowledge.server.query import (  # noqa: E402
    ASSUMED_ANY,
    COVERED,
    MISMATCH,
    STALE_WARNING,
    UNCHECKED,
    evaluate_dimension,
    explain,
    query,
)

TODAY = dt.date(2026, 9, 7)


def run(**kwargs):
    config = kwargs.pop("config", None) or support.build_config()
    kwargs.setdefault("today", TODAY)
    return query(config, **kwargs).to_dict()


class DefaultResultSet(unittest.TestCase):
    def test_default_is_shared_plus_project_verified_stale_resolved(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        got = set(support.uuids(payload))
        self.assertEqual(
            {support.SHARED_SOC_A, support.SHARED_STALE, support.SHARED_RESOLVED, support.PROJECT_ONLY},
            got,
        )
        self.assertEqual(["shared", "project"], payload["request"]["layers"])
        self.assertEqual(["verified", "stale", "resolved"], payload["request"]["statuses"])

    def test_deprecated_and_superseded_stay_out_of_default_results(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        got = support.uuids(payload)
        self.assertNotIn(support.SHARED_DEPRECATED, got)
        self.assertNotIn(support.SHARED_SUPERSEDED, got)

    def test_unverified_and_candidate_require_an_explicit_opt_in(self):
        default = support.uuids(run(reader_coordinate=support.READER_SOC_A))
        self.assertNotIn(support.CANDIDATE_ONLY, default)

        opted_in = run(reader_coordinate=support.READER_SOC_A, include_unverified=True)
        self.assertIn(support.CANDIDATE_ONLY, support.uuids(opted_in))
        self.assertIn("unverified", opted_in["request"]["statuses"])
        self.assertIn("candidate", opted_in["request"]["layers"])

        candidate = support.by_uuid(opted_in, support.CANDIDATE_ONLY)
        self.assertEqual("candidate", candidate["layer"])
        self.assertEqual("unverified", candidate["status"])
        self.assertTrue(any("layer=candidate" in w for w in candidate["warnings"]))
        self.assertTrue(any("status=unverified" in w for w in candidate["warnings"]))

    def test_explicit_status_filter_can_reach_deprecated_entries(self):
        payload = run(reader_coordinate=support.READER_SOC_A, statuses=["deprecated"])
        self.assertEqual([support.SHARED_DEPRECATED], support.uuids(payload))

    def test_every_result_carries_layer_status_confidence_and_origin_repo(self):
        payload = run(reader_coordinate=support.READER_SOC_A, include_unverified=True)
        self.assertTrue(payload["results"])
        for result in payload["results"]:
            self.assertIn(result["layer"], ("shared", "project", "candidate"))
            self.assertIn(
                result["status"], ("verified", "stale", "resolved", "unverified", "deprecated")
            )
            self.assertIn(result["confidence"], ("high", "medium", "low"))
            self.assertTrue(result["provenance"]["origin_repo"])
        self.assertEqual(
            "example-org/example-business-repo",
            support.by_uuid(payload, support.PROJECT_ONLY)["provenance"]["origin_repo"],
        )


class CoordinateMatching(unittest.TestCase):
    def test_other_soc_entry_is_withheld_and_the_withholding_is_reported(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        self.assertNotIn(support.SHARED_SOC_B, support.uuids(payload))
        self.assertTrue(
            any("falls outside their established scope" in note for note in payload["notes"]),
            payload["notes"],
        )

    def test_mismatch_is_visible_on_request_never_disguised(self):
        payload = run(reader_coordinate=support.READER_SOC_A, include_non_matching=True)
        soc_b = support.by_uuid(payload, support.SHARED_SOC_B)
        self.assertFalse(soc_b["applicability"]["applies"])
        mismatched = {m["dimension"] for m in soc_b["applicability"]["mismatched"]}
        self.assertEqual({"soc", "cann"}, mismatched)
        self.assertTrue(any("DOES NOT APPLY" in w for w in soc_b["warnings"]))
        # ...and it must never outrank an entry that does apply.
        self.assertLess(
            support.uuids(payload).index(support.SHARED_SOC_A),
            support.uuids(payload).index(support.SHARED_SOC_B),
        )

    def test_version_range_below_minimum_is_a_mismatch(self):
        reader = dict(support.READER_SOC_A, vllm="0.8.3")
        payload = run(reader_coordinate=reader, include_non_matching=True)
        soc_a = support.by_uuid(payload, support.SHARED_SOC_A)
        self.assertFalse(soc_a["applicability"]["applies"])
        self.assertEqual(
            ["vllm"], [m["dimension"] for m in soc_a["applicability"]["mismatched"]]
        )
        self.assertIn("below the established minimum", soc_a["applicability"]["mismatched"][0]["detail"])

    def test_execution_mode_outside_the_entry_scope_withholds_it(self):
        # The stale entry was established under aclgraph capture only.
        reader = dict(support.READER_SOC_A, execution_mode="eager")
        payload = run(reader_coordinate=reader)
        self.assertNotIn(support.SHARED_STALE, support.uuids(payload))
        with_mismatches = run(reader_coordinate=reader, include_non_matching=True)
        stale = support.by_uuid(with_mismatches, support.SHARED_STALE)
        self.assertEqual(
            ["execution_mode"], [m["dimension"] for m in stale["applicability"]["mismatched"]]
        )

    def test_resolved_entry_range_upper_bound_is_enforced(self):
        # dd44 was established on vllm_ascend 0.0.0..0.0.1; a reader past that
        # bound is outside the entry's coordinate.
        reader = dict(support.READER_SOC_A, vllm_ascend="0.1.0")
        payload = run(reader_coordinate=reader)
        self.assertNotIn(support.SHARED_RESOLVED, support.uuids(payload))

    def test_any_dimensions_are_surfaced_as_unproven_assumptions(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        soc_a = support.by_uuid(payload, support.SHARED_SOC_A)
        assumed = {
            item["dimension"]: item["basis"]
            for item in soc_a["applicability"]["matched_on_independence_claim"]
        }
        self.assertIn("cann", assumed)
        self.assertIn("driver", assumed)
        self.assertTrue(assumed["cann"])
        self.assertTrue(
            any("unproven independence claim" in w for w in soc_a["warnings"]), soc_a["warnings"]
        )
        # A bounded dimension the reader satisfies is a different thing.
        self.assertIn("soc", soc_a["applicability"]["covered"])
        self.assertIn("topology", soc_a["applicability"]["covered"])

    def test_dimensions_the_reader_did_not_supply_come_back_unchecked(self):
        payload = run(reader_coordinate={"soc": "ExampleSoC-A"})
        soc_a = support.by_uuid(payload, support.SHARED_SOC_A)
        unchecked = set(soc_a["applicability"]["unchecked"])
        self.assertIn("topology", unchecked)
        self.assertIn("component", unchecked)
        self.assertIn("vllm", unchecked)
        self.assertTrue(any("not checked against your build" in n for n in soc_a["notes"]))
        self.assertEqual(["soc"], soc_a["applicability"]["covered"])
        self.assertTrue(soc_a["applicability"]["applies"])

    def test_empty_coordinate_checks_nothing_and_says_so(self):
        payload = run()
        self.assertEqual({}, payload["reader_coordinate"]["supplied"])
        self.assertEqual(10, len(payload["reader_coordinate"]["unsupplied_expected_dimensions"]))
        soc_b = support.by_uuid(payload, support.SHARED_SOC_B)
        self.assertTrue(soc_b["applicability"]["applies"])
        self.assertIn("soc", soc_b["applicability"]["unchecked"])

    def test_unknown_coordinate_keys_are_reported_not_silently_dropped(self):
        payload = run(reader_coordinate={"soc": "ExampleSoC-A", "phase_of_moon": "waxing"})
        self.assertEqual(["phase_of_moon"], payload["reader_coordinate"]["ignored_keys"])

    def test_ranking_prefers_bounded_coverage_over_independence_claims(self):
        # 1122 (project) claims `any` on ten dimensions; aa11 (shared) bounds
        # soc, torch, vllm, topology and component and the reader satisfies
        # all of them. The observed coordinate must rank first.
        payload = run(reader_coordinate=support.READER_SOC_A)
        order = support.uuids(payload)
        self.assertLess(order.index(support.SHARED_SOC_A), order.index(support.PROJECT_ONLY))


class DimensionVerdicts(unittest.TestCase):
    def test_values_match_is_case_insensitive_but_not_fuzzy(self):
        constraint = {"values": ["tp8", "tp4"]}
        self.assertEqual(COVERED, evaluate_dimension("topology", constraint, "TP8").verdict)
        self.assertEqual(MISMATCH, evaluate_dimension("topology", constraint, "tp16").verdict)

    def test_unorderable_version_is_undecidable_not_a_match(self):
        verdict = evaluate_dimension("torch", {"range": {"min": "2.5.0", "max": None}}, "unknown")
        self.assertEqual("undecidable", verdict.verdict)
        self.assertIn("cannot order", verdict.detail)

    def test_release_candidate_sorts_below_the_release(self):
        constraint = {"range": {"min": "8.1.0", "max": None}}
        self.assertEqual(MISMATCH, evaluate_dimension("cann", constraint, "8.1.RC1").verdict)
        self.assertEqual(COVERED, evaluate_dimension("cann", constraint, "8.1.0").verdict)

    def test_build_metadata_is_ignored_for_ordering(self):
        constraint = {"range": {"min": "0.9.0", "max": None}}
        self.assertEqual(COVERED, evaluate_dimension("vllm", constraint, "0.9.1+example").verdict)

    def test_any_matches_even_with_no_reader_value_but_stays_an_assumption(self):
        verdict = evaluate_dimension("soc", {"any": True, "basis": "x" * 20}, None)
        self.assertEqual(ASSUMED_ANY, verdict.verdict)
        self.assertIn("supplied no value", verdict.detail)

    def test_dimension_the_entry_never_declared_is_undecidable(self):
        # schema v2 forbids this shape, but the service reads files it did not
        # write; the answer is "unknown", never "matches".
        verdict = evaluate_dimension("soc", {"unexpected": "shape"}, "ExampleSoC-A")
        self.assertEqual("undecidable", verdict.verdict)

    def test_missing_reader_value_on_a_bounded_dimension_is_unchecked(self):
        verdict = evaluate_dimension("topology", {"values": ["tp8"]}, None)
        self.assertEqual(UNCHECKED, verdict.verdict)


class Lifecycle(unittest.TestCase):
    def test_stale_entry_comes_back_with_its_warning(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        stale = support.by_uuid(payload, support.SHARED_STALE)
        self.assertEqual("stale", stale["status"])
        self.assertIn(STALE_WARNING, stale["warnings"])
        self.assertIn("Do not trust its version bounds", STALE_WARNING)
        self.assertEqual("2026-01-05", stale["staleness"]["last_verified_at"])
        self.assertEqual(245, stale["staleness"]["age_days"])

    def test_resolved_entry_carries_its_fix_reference(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        resolved = support.by_uuid(payload, support.SHARED_RESOLVED)
        self.assertEqual(
            {"type": "pull_request", "ref": "example-org/example-repo#77"},
            resolved["lifecycle"]["resolved_by"],
        )
        self.assertTrue(any("status=resolved" in w for w in resolved["warnings"]))
        self.assertTrue(any("example-org/example-repo#77" in w for w in resolved["warnings"]))

    def test_verified_entry_past_the_policy_horizon_says_the_sweep_has_not_run(self):
        config = support.build_config(policy={"stale_after_days": 3})
        payload = run(config=config, reader_coordinate=support.READER_SOC_A)
        soc_a = support.by_uuid(payload, support.SHARED_SOC_A)
        self.assertTrue(soc_a["staleness"]["policy_horizon_exceeded"])
        self.assertTrue(any("staleness sweep has" in w for w in soc_a["warnings"]))


class LayerPrecedence(unittest.TestCase):
    def test_shared_revision_wins_and_the_local_divergence_is_reported(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        soc_a = support.by_uuid(payload, support.SHARED_SOC_A)
        self.assertEqual("shared", soc_a["layer"])
        self.assertEqual(1, len(soc_a["also_present_in"]))
        other = soc_a["also_present_in"][0]
        self.assertEqual("project", other["layer"])
        self.assertIn("revision_divergence", other)
        self.assertNotEqual(soc_a["content_hash"], other["content_hash"])

    def test_project_layer_alone_still_answers(self):
        config = support.build_config(shared="missing")
        payload = run(config=config, reader_coordinate=support.READER_SOC_A)
        self.assertEqual(
            {support.PROJECT_ONLY, support.SHARED_SOC_A}, set(support.uuids(payload))
        )
        self.assertEqual(
            "project", support.by_uuid(payload, support.SHARED_SOC_A)["layer"]
        )
        self.assertTrue(payload["degraded"])
        self.assertIn("does not exist", payload["layers_absent"]["shared"])

    def test_project_results_are_labelled_as_possibly_unpublishable(self):
        payload = run(reader_coordinate=support.READER_SOC_A)
        project = support.by_uuid(payload, support.PROJECT_ONLY)
        self.assertTrue(any("layer=project" in n for n in project["notes"]))

    def test_layers_argument_restricts_the_search(self):
        payload = run(reader_coordinate=support.READER_SOC_A, layers=["project"])
        self.assertEqual({"project"}, {r["layer"] for r in payload["results"]})


class TextAndFingerprintMatching(unittest.TestCase):
    def test_fingerprint_match_is_reported_on_the_result(self):
        payload = run(
            fingerprint="example hostname resolution failed",
            reader_coordinate=support.READER_SOC_A,
        )
        self.assertEqual([support.SHARED_SOC_A], support.uuids(payload))
        self.assertIn(
            "example hostname resolution failed",
            support.by_uuid(payload, support.SHARED_SOC_A)["match"]["matched_fingerprints"],
        )

    def test_free_text_finds_the_entry_and_records_the_matched_terms(self):
        payload = run(text="graph capture timeout at startup", reader_coordinate=support.READER_SOC_A)
        self.assertIn(support.SHARED_STALE, support.uuids(payload))
        self.assertTrue(support.by_uuid(payload, support.SHARED_STALE)["match"]["matched_terms"])

    def test_no_match_means_unknown_not_nothing_to_worry_about(self):
        payload = run(text="zzz nonexistent symptom zzz", reader_coordinate=support.READER_SOC_A)
        self.assertEqual([], payload["results"])
        self.assertEqual(0, payload["count"])
        self.assertEqual("unknown", payload["absent_fact_semantics"])
        self.assertIn("UNKNOWN", payload["no_result_meaning"])

    def test_limit_is_honoured(self):
        payload = run(reader_coordinate=support.READER_SOC_A, limit=2)
        self.assertEqual(2, len(payload["results"]))


class ServiceUnavailable(unittest.TestCase):
    def test_no_layers_mounted_returns_unknown_not_an_error(self):
        config = support.build_config(shared=False, project=False, candidate=False)
        payload = run(config=config, reader_coordinate=support.READER_SOC_A)
        self.assertEqual([], payload["results"])
        self.assertTrue(payload["degraded"])
        self.assertEqual([], payload["layers_available"])
        self.assertEqual({"shared", "project", "candidate"}, set(payload["layers_absent"]))
        self.assertTrue(any("'unknown', never 'supported'" in n for n in payload["notes"]))


class Explain(unittest.TestCase):
    def test_explain_returns_the_full_record_with_evidence_and_coordinate(self):
        config = support.build_config()
        payload = explain(
            config,
            support.SHARED_SOC_A,
            reader_coordinate=support.READER_SOC_A,
            today=TODAY,
        )
        self.assertTrue(payload["found"])
        self.assertEqual("shared", payload["layer"])
        self.assertEqual(12, len(payload["entry"]["scope"]))
        self.assertEqual(
            "example-run-0001", payload["entry"]["verification"]["evidence"][0]["ref"]
        )
        self.assertEqual(
            "ExampleSoC-A", payload["entry"]["verification"]["verified_against"]["soc"]
        )
        self.assertTrue(payload["applicability"]["applies"])
        self.assertTrue(payload["applicability"]["matched_on_independence_claim"])
        self.assertEqual(1, len(payload["other_copies"]))
        self.assertEqual("project", payload["other_copies"][0]["layer"])

    def test_explain_reaches_entries_default_query_hides(self):
        config = support.build_config()
        payload = explain(config, support.SHARED_DEPRECATED, today=TODAY)
        self.assertTrue(payload["found"])
        self.assertEqual("deprecated", payload["entry"]["status"])

    def test_explain_of_an_unknown_uuid_is_unknown_not_false(self):
        config = support.build_config()
        payload = explain(config, "00000000-0000-4000-8000-000000000000", today=TODAY)
        self.assertFalse(payload["found"])
        self.assertIn("unknown", payload["meaning"])
        self.assertEqual("unknown", payload["absent_fact_semantics"])
        self.assertEqual(["shared", "project", "candidate"], payload["layers_consulted"])

    def test_explain_says_which_layers_were_missing(self):
        config = support.build_config(shared="missing", project=False)
        payload = explain(config, support.SHARED_SOC_A, today=TODAY)
        self.assertFalse(payload["found"])
        self.assertTrue(payload["degraded"])
        self.assertIn("shared", payload["layers_absent"])
        self.assertEqual(["candidate"], payload["layers_consulted"])


if __name__ == "__main__":
    unittest.main()
