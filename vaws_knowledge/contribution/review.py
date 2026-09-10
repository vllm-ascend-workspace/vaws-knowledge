"""Trusted review: recall related published docs, classify, bind identities.

Decisions: new, duplicate, supplement, condition_difference, conflict,
insufficient_evidence. External unavailability is not a pass. Results bind
candidate head + base SHA + related path/Git SHA identities.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from vaws_knowledge.contribution.documents import (
    ContentIdentity,
    MarkdownDocument,
    content_digest,
    require_git_sha,
)
from vaws_knowledge.contribution.errors import IdentityError
from vaws_knowledge.contribution.grok import Classification, ScriptedClassifier
from vaws_knowledge.contribution.recall import Recall, RecallResult, RelatedDocument
from vaws_knowledge.contribution.rewrite import RewriteGenerator

DECISION_NEW = "new"
DECISION_DUPLICATE = "duplicate"
DECISION_SUPPLEMENT = "supplement"
DECISION_CONDITION = "condition_difference"
DECISION_CONFLICT = "conflict"
DECISION_EVIDENCE = "insufficient_evidence"

ACTION_MERGE = "merge"
ACTION_CLOSE_DUP = "close_duplicate"
ACTION_HOLD_SUPPLEMENT = "hold_supplement"
ACTION_HOLD_CONFLICT = "await_human_decision"
ACTION_AWAIT_DECISION = "await_human_decision"
ACTION_HOLD_EVIDENCE = "hold_evidence"
ACTION_HOLD = "hold"
ACTION_RE_REVIEW = "re_review"
ACTION_UNAVAILABLE = "unavailable"

REVIEW_NOTES = (
    "Public review records responsibility for published text; it does not prove hardware facts.",
    "Grok did not reproduce hardware measurements.",
    "Local experience and public knowledge are both reference, not axioms.",
    "Comparison uses relevance and known conditions, not a trust rank or unreviewed filter.",
    "Unknown applicability is not an automatic exclusion.",
)

_STRONG_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:tflops|gflops|tokens/?s|gb/s|tb/s|ms\b|µs|μs)",
    re.IGNORECASE,
)
_UNIVERSAL = re.compile(
    r"(always|never|all versions|every device|所有版本|始终|从不|峰值是)",
    re.IGNORECASE,
)
_EVIDENCE = re.compile(
    r"(evidence|measured|measurement|log:|日志|实测|证据|table|benchmark report)",
    re.IGNORECASE,
)
_STRENGTH_RANK = {
    "ordinary_observation": 0,
    "strong_metric": 1,
    "universal_conclusion": 2,
}


@dataclass
class ReviewResult:
    status: str
    decision: str | None
    action: str
    reason: str
    candidate_head: str
    base_sha: str
    related: list[ContentIdentity] = field(default_factory=list)
    claim_strength: str | None = None
    scope_note: str | None = None
    evidence_commensurate: bool | None = None
    permits_publish: bool = False
    supplement_diff: str | None = None
    rewrite_applied: bool = False
    conflict_key: str | None = None
    notes: list[str] = field(default_factory=list)
    recall: dict[str, Any] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)
    title: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "action": self.action,
            "reason": self.reason,
            "candidate_head": self.candidate_head,
            "base_sha": self.base_sha,
            "related": [item.as_dict() for item in self.related],
            "claim_strength": self.claim_strength,
            "scope_note": self.scope_note,
            "evidence_commensurate": self.evidence_commensurate,
            "permits_publish": self.permits_publish,
            "supplement_diff": self.supplement_diff,
            "rewrite_applied": self.rewrite_applied,
            "conflict_key": self.conflict_key,
            "notes": list(self.notes),
            "recall": dict(self.recall),
            "classification": dict(self.classification),
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewResult":
        related = []
        for item in payload.get("related") or []:
            if isinstance(item, dict):
                related.append(ContentIdentity(path=str(item["path"]), git_sha=str(item["git_sha"])))
        return cls(
            status=str(payload.get("status") or "error"),
            decision=payload.get("decision") if isinstance(payload.get("decision"), str) else None,
            action=str(payload.get("action") or ACTION_HOLD),
            reason=str(payload.get("reason") or ""),
            candidate_head=require_git_sha(payload.get("candidate_head")),
            base_sha=require_git_sha(payload.get("base_sha")),
            related=related,
            claim_strength=payload.get("claim_strength") if isinstance(payload.get("claim_strength"), str) else None,
            scope_note=payload.get("scope_note") if isinstance(payload.get("scope_note"), str) else None,
            evidence_commensurate=payload.get("evidence_commensurate")
            if isinstance(payload.get("evidence_commensurate"), bool)
            else None,
            permits_publish=bool(payload.get("permits_publish")),
            supplement_diff=payload.get("supplement_diff") if isinstance(payload.get("supplement_diff"), str) else None,
            rewrite_applied=bool(payload.get("rewrite_applied")),
            conflict_key=payload.get("conflict_key") if isinstance(payload.get("conflict_key"), str) else None,
            notes=list(payload.get("notes") or []),
            recall=dict(payload.get("recall") or {}),
            classification=dict(payload.get("classification") or {}),
            title=str(payload.get("title") or ""),
        )


def infer_claim_strength(title: str, body: str) -> str:
    text = f"{title}\n{body}"
    if _STRONG_UNIT.search(text):
        return "strong_metric"
    if _UNIVERSAL.search(text):
        return "universal_conclusion"
    return "ordinary_observation"


def has_commensurate_evidence(title: str, body: str, strength: str) -> bool:
    if strength == "ordinary_observation":
        return True
    return bool(_EVIDENCE.search(f"{title}\n{body}"))


def stronger_strength(left: str | None, right: str | None) -> str:
    best = "ordinary_observation"
    for item in (left, right):
        if item in _STRENGTH_RANK and _STRENGTH_RANK[item] > _STRENGTH_RANK[best]:
            best = item
    return best


def conflict_fingerprint(candidate: MarkdownDocument, related: Sequence[RelatedDocument]) -> str:
    parts = [candidate.digest]
    for item in sorted(related, key=lambda doc: doc.identity.path):
        parts.append(f"{item.identity.path}:{content_digest(item.title, item.body)}")
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def exact_duplicate(candidate: MarkdownDocument, related: Sequence[RelatedDocument]) -> RelatedDocument | None:
    for item in related:
        if item.document().digest == candidate.digest:
            return item
        if item.title.strip() == candidate.title.strip() and item.body.strip() == candidate.body.strip():
            return item
    return None


def supplement_diff(existing: RelatedDocument, incoming: MarkdownDocument) -> str:
    before = f"# {existing.title}\n\n{existing.body.strip()}\n".splitlines(keepends=True)
    after = incoming.render().splitlines(keepends=True)
    diff = difflib.unified_diff(
        before,
        after,
        fromfile=existing.identity.path,
        tofile="candidate",
        lineterm="\n",
    )
    return "".join(diff)


def _action_for(decision: str, permits: bool) -> str:
    if decision == DECISION_NEW and permits:
        return ACTION_MERGE
    if decision == DECISION_CONDITION and permits:
        return ACTION_MERGE
    if decision == DECISION_DUPLICATE:
        return ACTION_CLOSE_DUP
    if decision == DECISION_SUPPLEMENT:
        return ACTION_HOLD_SUPPLEMENT
    if decision == DECISION_CONFLICT:
        return ACTION_AWAIT_DECISION
    if decision == DECISION_EVIDENCE:
        return ACTION_HOLD_EVIDENCE
    return ACTION_HOLD


def _unavailable(
    *,
    head: str,
    base: str,
    reason: str,
    related: list[ContentIdentity],
    recall: RecallResult | None = None,
    classification: Classification | None = None,
    title: str = "",
) -> ReviewResult:
    return ReviewResult(
        status="unavailable",
        decision=None,
        action=ACTION_UNAVAILABLE,
        reason=reason,
        candidate_head=head,
        base_sha=base,
        related=related,
        permits_publish=False,
        notes=list(REVIEW_NOTES),
        recall=recall.as_dict() if recall is not None else {},
        classification=classification.as_dict() if classification is not None else {},
        title=title,
    )


def review_candidate(
    text: str,
    *,
    candidate_head: str,
    base_sha: str,
    recall: Recall,
    classifier: Any | None = None,
    rewrite: RewriteGenerator | None = None,
    path: str = "corpus/candidate.md",
    accepted_decision: Any | None = None,
    rewrite_applied: bool = False,
    addressed_paths: set[str] | None = None,
    remaining_related: Sequence[RelatedDocument] | None = None,
) -> ReviewResult:
    """Classify one candidate against related published documents."""

    head = require_git_sha(candidate_head)
    base = require_git_sha(base_sha)
    if head == base:
        # A PR head may theoretically equal an old base only in tests; allowed.
        pass
    document = MarkdownDocument.from_text(text)
    recalled = recall.related(f"{document.title}\n{document.body}")
    recalled_docs = list(recalled.documents)
    if remaining_related is not None:
        recalled_docs = list(remaining_related)
    if addressed_paths:
        recalled_docs = [item for item in recalled_docs if item.identity.path not in addressed_paths]
    related_ids = [item.identity for item in recalled_docs]
    if remaining_related is None and not recalled.ok:
        return _unavailable(
            head=head,
            base=base,
            reason=recalled.reason or "OpenViking recall unavailable",
            related=related_ids,
            recall=recalled,
            title=document.title,
        )
    if rewrite_applied and accepted_decision is not None and not recalled_docs:
        return ReviewResult(
            status="success",
            decision=DECISION_NEW,
            action=ACTION_MERGE,
            reason="human direction already applied; expected rewrite head re-checked",
            candidate_head=head,
            base_sha=base,
            related=related_ids,
            claim_strength=infer_claim_strength(document.title, document.body),
            scope_note="human direction already applied; expected rewrite head re-checked",
            evidence_commensurate=True,
            permits_publish=True,
            rewrite_applied=True,
            conflict_key=getattr(accepted_decision, "conflict_key", None),
            notes=list(REVIEW_NOTES),
            recall=recalled.as_dict(),
            classification={"status": "success", "decision": DECISION_NEW, "provider_called": False},
            title=document.title,
        )
    dup = exact_duplicate(document, recalled_docs)
    if dup is not None:
        return ReviewResult(
            status="success",
            decision=DECISION_DUPLICATE,
            action=ACTION_CLOSE_DUP,
            reason=f"exact duplicate of {dup.identity.label()}",
            candidate_head=head,
            base_sha=base,
            related=related_ids,
            claim_strength=infer_claim_strength(document.title, document.body),
            scope_note="duplicate",
            evidence_commensurate=True,
            permits_publish=False,
            notes=list(REVIEW_NOTES),
            recall=recalled.as_dict(),
            classification={"status": "success", "decision": DECISION_DUPLICATE, "provider_called": False},
            title=document.title,
        )
    if classifier is None:
        classifier = ScriptedClassifier(
            Classification(status="unavailable", reason="classifier not configured")
        )
    classified = classifier.classify(document, recalled_docs)
    if classified.status != "success" or classified.decision is None:
        status = classified.status if classified.status in {"unavailable", "error"} else "error"
        return ReviewResult(
            status=status,
            decision=None,
            action=ACTION_UNAVAILABLE if status == "unavailable" else ACTION_HOLD,
            reason=classified.reason or "classifier failed",
            candidate_head=head,
            base_sha=base,
            related=related_ids,
            permits_publish=False,
            notes=list(REVIEW_NOTES),
            recall=recalled.as_dict(),
            classification=classified.as_dict(),
            title=document.title,
        )
    heuristic = infer_claim_strength(document.title, document.body)
    strength = stronger_strength(classified.claim_strength, heuristic)
    commensurate = has_commensurate_evidence(document.title, document.body, strength)
    if classified.evidence_commensurate is False:
        commensurate = False
    decision = classified.decision
    reason = classified.reason
    if strength != "ordinary_observation" and not commensurate:
        decision = DECISION_EVIDENCE
        reason = (
            classified.reason
            + "; strong metric or universal conclusion is not commensurate with given evidence"
        )
    permits = decision in {DECISION_NEW, DECISION_CONDITION} and (
        strength == "ordinary_observation" or commensurate
    )
    if decision == DECISION_NEW and strength == "ordinary_observation":
        scope = classified.scope_note or "reporter-observation; coordinates not required"
    elif decision == DECISION_CONDITION:
        scope = classified.scope_note or "condition-difference; both documents may remain"
        permits = True
    else:
        scope = classified.scope_note or None
    if decision == DECISION_CONDITION:
        permits = True
    if decision in {DECISION_DUPLICATE, DECISION_SUPPLEMENT, DECISION_CONFLICT, DECISION_EVIDENCE}:
        permits = False
    diff = None
    if decision == DECISION_SUPPLEMENT and recalled_docs:
        target = recalled_docs[0]
        for item in recalled_docs:
            if item.identity.label() in classified.related_ids:
                target = item
                break
        diff = supplement_diff(target, document)
    applied = bool(rewrite_applied)
    key = None
    if decision == DECISION_CONFLICT:
        key = conflict_fingerprint(document, recalled_docs)
        accepted_key = getattr(accepted_decision, "conflict_key", None)
        if applied and accepted_key and accepted_key == key:
            permits = True
            decision = DECISION_NEW
            scope = "human direction already applied; expected rewrite head re-checked"
        elif applied and accepted_key and accepted_key != key:
            permits = False
        elif accepted_key and accepted_key == key:
            permits = False
            scope = "human direction already recorded; do not re-ask"
        else:
            permits = False
    return ReviewResult(
        status="success",
        decision=decision,
        action=_action_for(decision, permits),
        reason=reason,
        candidate_head=head,
        base_sha=base,
        related=related_ids,
        claim_strength=strength,
        scope_note=scope,
        evidence_commensurate=commensurate,
        permits_publish=permits,
        supplement_diff=diff,
        rewrite_applied=applied,
        conflict_key=key,
        notes=list(REVIEW_NOTES),
        recall=recalled.as_dict(),
        classification=classified.as_dict(),
        title=document.title,
    )


def review_is_bound_to(result: ReviewResult, *, head: str, base: str) -> bool:
    try:
        return result.candidate_head == require_git_sha(head) and result.base_sha == require_git_sha(base)
    except IdentityError:
        return False
