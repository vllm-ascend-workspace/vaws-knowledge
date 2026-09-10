"""Stale head/base, concurrent PRs, merge CAS, duplicate close."""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from contribution.support import (  # noqa: E402
    BASE_1,
    HEAD_A,
    HEAD_B,
    OWNER_REPO,
    FakeContributionGitHub,
    git_sha,
)
from vaws_knowledge.contribution.merge import (  # noqa: E402
    MemorySerializer,
    close_duplicate,
    merge_reviewed,
)
from vaws_knowledge.contribution.review import (  # noqa: E402
    ACTION_CLOSE_DUP,
    ACTION_HOLD_CONFLICT,
    ACTION_MERGE,
    ACTION_RE_REVIEW,
    ReviewResult,
)


def _publishable(head: str, base: str, title: str = "note") -> ReviewResult:
    return ReviewResult(
        status="success",
        decision="new",
        action=ACTION_MERGE,
        reason="new ordinary observation",
        candidate_head=head,
        base_sha=base,
        permits_publish=True,
        title=title,
        notes=["Public review records responsibility for published text; it does not prove hardware facts."],
    )


class MergeBinding(unittest.TestCase):
    def test_matching_head_and_base_merges(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=7, head=HEAD_A, base=BASE_1)
        outcome = merge_reviewed(
            _publishable(HEAD_A, BASE_1),
            github=github,
            repository=OWNER_REPO,
            pr_number=7,
            serializer=MemorySerializer(),
        )
        self.assertTrue(outcome.merged)
        self.assertEqual(outcome.action, "merged")
        self.assertNotEqual(github.refs["main"], BASE_1)

    def test_stale_head_refuses_and_does_not_merge(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=7, head=HEAD_B, base=BASE_1)
        outcome = merge_reviewed(
            _publishable(HEAD_A, BASE_1),
            github=github,
            repository=OWNER_REPO,
            pr_number=7,
        )
        self.assertFalse(outcome.merged)
        self.assertEqual(outcome.action, ACTION_RE_REVIEW)
        self.assertEqual(outcome.reason, "stale head")
        self.assertEqual(github.refs["main"], BASE_1)

    def test_stale_base_refuses(self):
        github = FakeContributionGitHub(base_sha=git_sha("moved"))
        github.add_pull(number=7, head=HEAD_A, base=BASE_1)
        # live PR still names old base, default branch has moved
        outcome = merge_reviewed(
            _publishable(HEAD_A, BASE_1),
            github=github,
            repository=OWNER_REPO,
            pr_number=7,
        )
        self.assertFalse(outcome.merged)
        self.assertEqual(outcome.action, ACTION_RE_REVIEW)
        self.assertEqual(outcome.reason, "stale base")

    def test_two_concurrent_prs_cannot_both_land_on_the_old_base(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=1, head=HEAD_A, base=BASE_1, branch="contrib/one")
        github.add_pull(number=2, head=HEAD_B, base=BASE_1, branch="contrib/two")
        first = merge_reviewed(
            _publishable(HEAD_A, BASE_1, title="one"),
            github=github,
            repository=OWNER_REPO,
            pr_number=1,
        )
        second = merge_reviewed(
            _publishable(HEAD_B, BASE_1, title="two"),
            github=github,
            repository=OWNER_REPO,
            pr_number=2,
        )
        self.assertTrue(first.merged)
        self.assertFalse(second.merged)
        self.assertEqual(second.action, ACTION_RE_REVIEW)
        self.assertEqual(second.reason, "stale base")

    def test_conflict_review_does_not_merge(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=3, head=HEAD_A, base=BASE_1)
        review = ReviewResult(
            status="success",
            decision="conflict",
            action=ACTION_HOLD_CONFLICT,
            reason="same conditions, opposite claims",
            candidate_head=HEAD_A,
            base_sha=BASE_1,
            permits_publish=False,
        )
        outcome = merge_reviewed(review, github=github, repository=OWNER_REPO, pr_number=3)
        self.assertFalse(outcome.merged)
        self.assertEqual(github.refs["main"], BASE_1)

    def test_unavailable_review_is_not_a_pass(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=4, head=HEAD_A, base=BASE_1)
        review = ReviewResult(
            status="unavailable",
            decision=None,
            action="unavailable",
            reason="OpenViking recall failed",
            candidate_head=HEAD_A,
            base_sha=BASE_1,
            permits_publish=False,
        )
        outcome = merge_reviewed(review, github=github, repository=OWNER_REPO, pr_number=4)
        self.assertFalse(outcome.merged)

    def test_permission_error_is_explicit(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=5, head=HEAD_A, base=BASE_1)
        github.auth_fail = True
        outcome = merge_reviewed(
            _publishable(HEAD_A, BASE_1),
            github=github,
            repository=OWNER_REPO,
            pr_number=5,
        )
        self.assertFalse(outcome.merged)
        self.assertIn("permission", outcome.reason.lower())

    def test_duplicate_closes_without_merge(self):
        github = FakeContributionGitHub(base_sha=BASE_1)
        github.add_pull(number=8, head=HEAD_A, base=BASE_1)
        review = ReviewResult(
            status="success",
            decision="duplicate",
            action=ACTION_CLOSE_DUP,
            reason="exact duplicate of shared/graph.md@" + BASE_1,
            candidate_head=HEAD_A,
            base_sha=BASE_1,
            permits_publish=False,
        )
        closed = close_duplicate(github=github, repository=OWNER_REPO, pr_number=8, review=review)
        self.assertTrue(closed["closed"])
        self.assertEqual(github.pulls[8]["state"], "closed")
        self.assertEqual(github.refs["main"], BASE_1)
        puts = [item for item in github.calls if item[0] == "PUT"]
        self.assertEqual(puts, [])


if __name__ == "__main__":
    unittest.main()
