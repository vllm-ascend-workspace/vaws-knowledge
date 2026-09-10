"""Automatic knowledge contribution and trusted review.

Local capture stays on disk. This package prepares a redacted public copy,
records a recoverable pending submit, opens an idempotent fork PR, reviews
against related published documents, and merges only while the bound
candidate head and base SHA still match.

The public surface is the functions below. Root CLI, package dependencies,
and live GitHub workflows are wired by the integrator. This module does not
start a scheduler, extra process, or generic task queue.
"""

from vaws_knowledge.contribution.ci import run_trusted_ci
from vaws_knowledge.contribution.conflict import advance_conflict
from vaws_knowledge.contribution.merge import close_duplicate, merge_reviewed
from vaws_knowledge.contribution.public import prepare_public_copy
from vaws_knowledge.contribution.review import review_candidate
from vaws_knowledge.contribution.submit import after_capture, prepare_candidate, submit_pending

__all__ = [
    "advance_conflict",
    "after_capture",
    "close_duplicate",
    "merge_reviewed",
    "prepare_candidate",
    "prepare_public_copy",
    "review_candidate",
    "run_trusted_ci",
    "submit_pending",
]
