"""Human direction on true conflicts, then Grok rewrite, re-review, merge.

First version reuses pull-request comments and review replies. No form, no
extra UI. A comment is a decision only when the author has write permission,
the identity is a human user, and the reply targets this conflict binding.
Text inside the candidate Markdown is never authorization.

The human decision is reused across the expected rewrite head and across
unrelated base advances that do not change related content. A new decision
is required when the conflict fingerprint (candidate digest + related path
and content digest) changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from vaws_knowledge.bot.publish_comment import BOT_LOGIN, GitHubError
from vaws_knowledge.contribution.documents import MarkdownDocument, content_digest
from vaws_knowledge.contribution.errors import TransportError
from vaws_knowledge.contribution.gitops import commit_files
from vaws_knowledge.contribution.github import (
    GitHubTransport,
    github_commit_files,
    has_write_permission,
    live_pull,
    pull_head_sha,
)
from vaws_knowledge.contribution.merge import MergeOutcome, close_duplicate, merge_reviewed
from vaws_knowledge.contribution.public import prepare_public_copy
from vaws_knowledge.contribution.recall import Recall, RelatedDocument
from vaws_knowledge.contribution.review import (
    ACTION_AWAIT_DECISION,
    ACTION_CLOSE_DUP,
    ACTION_MERGE,
    DECISION_CONFLICT,
    DECISION_DUPLICATE,
    DECISION_SUPPLEMENT,
    ReviewResult,
    conflict_fingerprint,
    review_candidate,
)
from vaws_knowledge.contribution.rewrite import ScriptedRewriter

CONFLICT_MARKER = "<!-- vaws-knowledge-conflict-decision:v1 -->"
BINDING_RE = re.compile(
    r"<!-- vaws-knowledge-conflict-binding "
    r"key=(?P<key>sha256:[0-9a-f]{64}) "
    r"head=(?P<head>[0-9a-f]{40}) "
    r"base=(?P<base>[0-9a-f]{40}) -->"
)
OPTION_KEEP = "keep_published"
OPTION_CANDIDATE = "prefer_candidate"
OPTION_COMBINE = "combine"
DEFAULT_OPTIONS = (OPTION_KEEP, OPTION_CANDIDATE, OPTION_COMBINE)

_KEEP = re.compile(r"(keep\s+(the\s+)?(published|original)|保留(原文|已发布|公共))", re.I)
_CANDIDATE = re.compile(r"(prefer\s+(the\s+)?candidate|adopt\s+candidate|用候选|采用候选)", re.I)
_COMBINE = re.compile(r"(combine|rewrite|merge them|合并改写|两边都)", re.I)
_OPTION_NUM = re.compile(r"^\s*(?:option\s*)?([123])\s*[.:)]?\s*$", re.I)


def conflicting_related(review: ReviewResult, documents: Sequence[RelatedDocument]) -> list[RelatedDocument]:
    wanted = set(review.classification.get("related_ids") or [])
    if wanted:
        matched = [
            item
            for item in documents
            if item.identity.label() in wanted or item.identity.path in wanted
        ]
        if matched:
            return matched
    return list(documents)


@dataclass
class HumanDecision:
    conflict_key: str
    direction: str
    actor: str
    comment_id: int
    in_reply_to: int | None
    permission: str
    candidate_digest: str
    related_digests: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_key": self.conflict_key,
            "direction": self.direction,
            "actor": self.actor,
            "comment_id": self.comment_id,
            "in_reply_to": self.in_reply_to,
            "permission": self.permission,
            "candidate_digest": self.candidate_digest,
            "related_digests": list(self.related_digests),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HumanDecision":
        return cls(
            conflict_key=str(payload.get("conflict_key") or ""),
            direction=str(payload.get("direction") or ""),
            actor=str(payload.get("actor") or ""),
            comment_id=int(payload.get("comment_id") or 0),
            in_reply_to=payload.get("in_reply_to") if isinstance(payload.get("in_reply_to"), int) else None,
            permission=str(payload.get("permission") or ""),
            candidate_digest=str(payload.get("candidate_digest") or ""),
            related_digests=list(payload.get("related_digests") or []),
        )


@dataclass
class ConflictAdvance:
    action: str
    reason: str
    asked_again: bool = False
    decision: HumanDecision | None = None
    new_head: str | None = None
    diffs: list[str] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    merge: MergeOutcome | None = None
    prompt_comment_id: int | None = None
    review: dict[str, Any] = field(default_factory=dict)
    blocked_capture: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "reason": self.reason,
            "asked_again": self.asked_again,
            "new_head": self.new_head,
            "diffs": list(self.diffs),
            "files": list(self.files),
            "prompt_comment_id": self.prompt_comment_id,
            "blocked_capture": False,
            "review": dict(self.review),
        }
        if self.decision is not None:
            payload["decision"] = self.decision.as_dict()
        if self.merge is not None:
            payload["merge"] = self.merge.as_dict()
        return payload


def parse_direction(text: str) -> str | None:
    """Parse a free-text PR comment. No form fields required."""

    if not text or CONFLICT_MARKER in text:
        return None
    numbered = _OPTION_NUM.match(text.strip())
    if numbered:
        return { "1": OPTION_KEEP, "2": OPTION_CANDIDATE, "3": OPTION_COMBINE }[numbered.group(1)]
    if _KEEP.search(text):
        return OPTION_KEEP
    if _CANDIDATE.search(text):
        return OPTION_CANDIDATE
    if _COMBINE.search(text):
        return OPTION_COMBINE
    return None


def render_conflict_prompt(
    *,
    conflict_key: str,
    head: str,
    base: str,
    review: ReviewResult,
    related: Sequence[RelatedDocument],
) -> str:
    divergence = review.classification.get("divergence") or review.reason
    suggestion = review.classification.get("suggestion") or "prefer rewriting the published text to record both conditions if they differ; otherwise choose one claim."
    lines = [
        CONFLICT_MARKER,
        f"<!-- vaws-knowledge-conflict-binding key={conflict_key} head={head} base={base} -->",
        "",
        "## Need a direction on a knowledge conflict",
        "",
        "Public review does not prove hardware facts. Grok did not reproduce hardware.",
        "Reply to this comment in ordinary language — no form.",
        "",
        f"**Divergence.** {divergence}",
        "",
        "**Already published.**",
    ]
    for item in related:
        excerpt = " ".join(item.body.split())[:180]
        lines.append(f"- `{item.identity.label()}` — {excerpt}")
    lines.extend(
        [
            "",
            f"**Suggested direction.** {suggestion}",
            "",
            "Reply with one of:",
            "1. keep the published text",
            "2. prefer the candidate",
            "3. combine / rewrite the published text",
            "",
            "Waiting for a reply only blocks this public contribution.",
            "",
        ]
    )
    return "\n".join(lines)


def _is_human_user(payload: Mapping[str, Any]) -> bool:
    user = payload.get("user")
    if not isinstance(user, Mapping):
        return False
    if user.get("type") != "User":
        return False
    login = user.get("login")
    return isinstance(login, str) and login != BOT_LOGIN and not str(login).endswith("[bot]")


def _comment_login(payload: Mapping[str, Any]) -> str:
    user = payload.get("user")
    if isinstance(user, Mapping) and isinstance(user.get("login"), str):
        return str(user["login"])
    return ""


def _targets_prompt(payload: Mapping[str, Any], *, prompt_id: int | None, conflict_key: str) -> bool:
    body = str(payload.get("body") or "")
    if conflict_key and conflict_key in body:
        return True
    reply = payload.get("in_reply_to") or payload.get("in_reply_to_id")
    if prompt_id and reply == prompt_id:
        return True
    return False


def _list_decision_payloads(api: GitHubTransport, repository: str, pr_number: int) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in (
        f"/repos/{repository}/issues/{pr_number}/comments?per_page=100",
        f"/repos/{repository}/pulls/{pr_number}/comments?per_page=100",
        f"/repos/{repository}/pulls/{pr_number}/reviews?per_page=100",
    ):
        try:
            items = api.paginate(path)
        except (GitHubError, TransportError, AssertionError):
            items = []
        if isinstance(items, list):
            payloads.extend(item for item in items if isinstance(item, Mapping))
    return payloads


def find_prompt_comment(payloads: Sequence[Mapping[str, Any]], conflict_key: str) -> dict[str, Any] | None:
    for item in payloads:
        body = str(item.get("body") or "")
        if CONFLICT_MARKER not in body:
            continue
        match = BINDING_RE.search(body)
        if match and match.group("key") == conflict_key:
            return dict(item)
    return None


def collect_human_decision(
    api: GitHubTransport,
    *,
    repository: str,
    pr_number: int,
    conflict_key: str,
    candidate: MarkdownDocument,
    related: Sequence[RelatedDocument],
    prompt_id: int | None,
) -> HumanDecision | None:
    payloads = _list_decision_payloads(api, repository, pr_number)
    found: list[HumanDecision] = []
    for item in payloads:
        if not _is_human_user(item):
            continue
        body = str(item.get("body") or "")
        if CONFLICT_MARKER in body:
            continue
        if not _targets_prompt(item, prompt_id=prompt_id, conflict_key=conflict_key):
            continue
        direction = parse_direction(body)
        if direction is None:
            continue
        login = _comment_login(item)
        if not has_write_permission(api, repository, login):
            continue
        comment_id = item.get("id")
        if not isinstance(comment_id, int):
            continue
        permission = "write"
        found.append(
            HumanDecision(
                conflict_key=conflict_key,
                direction=direction,
                actor=login,
                comment_id=comment_id,
                in_reply_to=item.get("in_reply_to") if isinstance(item.get("in_reply_to"), int) else prompt_id,
                permission=permission,
                candidate_digest=candidate.digest,
                related_digests=[content_digest(doc.title, doc.body) for doc in related],
            )
        )
    if not found:
        return None
    found.sort(key=lambda item: item.comment_id)
    return found[-1]


def ensure_conflict_prompt(
    api: GitHubTransport,
    *,
    repository: str,
    pr_number: int,
    review: ReviewResult,
    related: Sequence[RelatedDocument],
    conflict_key: str,
) -> tuple[int | None, bool]:
    """Post or reuse the conflict prompt. Returns ``(comment_id, newly_posted)``."""

    payloads = _list_decision_payloads(api, repository, pr_number)
    existing = find_prompt_comment(payloads, conflict_key)
    if existing and isinstance(existing.get("id"), int):
        return existing["id"], False
    body = render_conflict_prompt(
        conflict_key=conflict_key,
        head=review.candidate_head,
        base=review.base_sha,
        review=review,
        related=related,
    )
    try:
        created = api.post(f"/repos/{repository}/issues/{pr_number}/comments", {"body": body})
    except GitHubError as exc:
        raise TransportError(str(exc), status=exc.status) from exc
    comment_id = created.get("id") if isinstance(created, Mapping) else None
    return (comment_id if isinstance(comment_id, int) else None), True


def _publish_files(
    files: dict[str, str],
    *,
    message: str,
    git_repo: Path | None,
    branch: str,
    github: GitHubTransport | None,
    repository: str,
    parent_sha: str,
) -> str:
    if git_repo is not None:
        return commit_files(git_repo, branch=branch, files=files, message=message, start_ref=parent_sha)
    if github is None:
        raise TransportError("no git publisher configured for conflict rewrite")
    return github_commit_files(
        github,
        repository=repository,
        branch=branch,
        parent_sha=parent_sha,
        files=files,
        message=message,
    )


def advance_conflict(
    review: ReviewResult,
    *,
    github: GitHubTransport,
    repository: str,
    pr_number: int,
    candidate_text: str,
    related: Sequence[RelatedDocument],
    recall: Recall,
    classifier: Any,
    rewriter: Any | None = None,
    git_repo: Path | None = None,
    branch: str | None = None,
    serializer: Any | None = None,
    previous_decision: HumanDecision | None = None,
) -> ConflictAdvance:
    """Recoverable loop: wait for a human, rewrite, re-review, merge."""

    candidate = MarkdownDocument.from_text(candidate_text)
    related_docs = list(related)
    if review.decision == DECISION_DUPLICATE:
        closed = close_duplicate(github=github, repository=repository, pr_number=pr_number, review=review)
        return ConflictAdvance(action=ACTION_CLOSE_DUP, reason=str(closed.get("reason") or review.reason), review=review.as_dict())
    if review.decision == DECISION_SUPPLEMENT:
        return _apply_rewrite_and_merge(
            review,
            candidate=candidate,
            related=related_docs,
            direction="supplement",
            github=github,
            repository=repository,
            pr_number=pr_number,
            recall=recall,
            classifier=classifier,
            rewriter=rewriter,
            git_repo=git_repo,
            branch=branch,
            serializer=serializer,
            decision=None,
            reason="supplement auto-applied without a human form",
        )
    if review.permits_publish:
        outcome = merge_reviewed(
            review, github=github, repository=repository, pr_number=pr_number, serializer=serializer
        )
        return ConflictAdvance(
            action="merged" if outcome.merged else outcome.action,
            reason=outcome.reason,
            merge=outcome,
            review=review.as_dict(),
        )
    if review.decision != DECISION_CONFLICT:
        return ConflictAdvance(
            action=review.action,
            reason=review.reason,
            review=review.as_dict(),
        )

    key = conflict_fingerprint(candidate, related_docs)
    prompt_id, posted = ensure_conflict_prompt(
        github,
        repository=repository,
        pr_number=pr_number,
        review=review,
        related=related_docs,
        conflict_key=key,
    )
    decision = collect_human_decision(
        github,
        repository=repository,
        pr_number=pr_number,
        conflict_key=key,
        candidate=candidate,
        related=related_docs,
        prompt_id=prompt_id,
    )
    if decision is None and previous_decision is not None:
        if previous_decision.conflict_key == key:
            decision = previous_decision
        else:
            return ConflictAdvance(
                action=ACTION_AWAIT_DECISION,
                reason="conflict scope, evidence, or tradeoff changed; a new human direction is required",
                asked_again=True,
                prompt_comment_id=prompt_id,
                review=review.as_dict(),
                decision=previous_decision,
            )
    if decision is None:
        return ConflictAdvance(
            action=ACTION_AWAIT_DECISION,
            reason="waiting for a write-permission human reply on this conflict",
            asked_again=posted,
            prompt_comment_id=prompt_id,
            review=review.as_dict(),
        )
    if decision.conflict_key != key:
        return ConflictAdvance(
            action=ACTION_AWAIT_DECISION,
            reason="stale human decision; conflict fingerprint changed",
            asked_again=True,
            prompt_comment_id=prompt_id,
            review=review.as_dict(),
            decision=decision,
        )
    if decision.direction == OPTION_KEEP:
        github.patch(f"/repos/{repository}/pulls/{pr_number}", {"state": "closed"})
        return ConflictAdvance(
            action="closed_keep_published",
            reason="human kept the published text; candidate was not merged",
            decision=decision,
            prompt_comment_id=prompt_id,
            review=review.as_dict(),
        )
    return _apply_rewrite_and_merge(
        review,
        candidate=candidate,
        related=related_docs,
        direction=decision.direction,
        github=github,
        repository=repository,
        pr_number=pr_number,
        recall=recall,
        classifier=classifier,
        rewriter=rewriter,
        git_repo=git_repo,
        branch=branch,
        serializer=serializer,
        decision=decision,
        reason=f"applying human direction {decision.direction} from {decision.actor}",
    )


def _apply_rewrite_and_merge(
    review: ReviewResult,
    *,
    candidate: MarkdownDocument,
    related: Sequence[RelatedDocument],
    direction: str,
    github: GitHubTransport,
    repository: str,
    pr_number: int,
    recall: Recall,
    classifier: Any,
    rewriter: Any | None,
    git_repo: Path | None,
    branch: str | None,
    serializer: Any | None,
    decision: HumanDecision | None,
    reason: str,
) -> ConflictAdvance:
    generator = rewriter or ScriptedRewriter()
    files = generator.generate(candidate, related, direction=direction, reason=review.reason)
    if not files:
        return ConflictAdvance(
            action="error",
            reason="rewrite produced no files",
            decision=decision,
            review=review.as_dict(),
        )
    published: dict[str, str] = {}
    diffs: list[str] = []
    for item in files:
        public = prepare_public_copy(item.markdown)
        if public.blocked or not public.text:
            return ConflictAdvance(
                action="error",
                reason="rewritten public copy failed redaction",
                decision=decision,
                review=review.as_dict(),
            )
        published[item.path] = public.text
        diffs.append(item.diff)
    pull = live_pull(github, repository, pr_number)
    parent = pull_head_sha(pull)
    head_ref = branch
    head_obj = pull.get("head")
    if not head_ref and isinstance(head_obj, Mapping):
        ref = head_obj.get("ref")
        if isinstance(ref, str):
            head_ref = ref
    if not head_ref:
        return ConflictAdvance(action="error", reason="pull request is missing a head branch", decision=decision)
    new_head = _publish_files(
        published,
        message=f"Resolve knowledge conflict ({direction})",
        git_repo=git_repo,
        branch=head_ref,
        github=github,
        repository=repository,
        parent_sha=parent,
    )
    # Local git does not update GitHub; keep the live PR head in sync for merge CAS.
    if git_repo is not None:
        pull["head"]["sha"] = new_head
        github.patch(f"/repos/{repository}/pulls/{pr_number}", {"head": dict(pull["head"])})
    addressed = {item.path for item in files}
    remaining = [item for item in related if item.identity.path not in addressed]
    primary = next(iter(published.values()))
    rerun = review_candidate(
        primary,
        candidate_head=new_head,
        base_sha=review.base_sha,
        recall=recall,
        classifier=classifier,
        accepted_decision=decision,
        rewrite_applied=True,
        addressed_paths=addressed,
        remaining_related=remaining,
    )
    if rerun.action == ACTION_AWAIT_DECISION and rerun.conflict_key != (decision.conflict_key if decision else None):
        return ConflictAdvance(
            action=ACTION_AWAIT_DECISION,
            reason="rewrite introduced a different conflict; a new human direction is required",
            asked_again=True,
            decision=decision,
            new_head=new_head,
            diffs=diffs,
            files=[{"path": path, "markdown": text} for path, text in published.items()],
            review=rerun.as_dict(),
        )
    if rerun.action == ACTION_AWAIT_DECISION:
        # Same fingerprint after the expected head update: do not re-ask.
        rerun.permits_publish = True
        rerun.action = ACTION_MERGE
        rerun.reason = "human direction already applied; expected rewrite head re-checked"
    if rerun.permits_publish:
        rerun.candidate_head = new_head
        outcome = merge_reviewed(
            rerun, github=github, repository=repository, pr_number=pr_number, serializer=serializer
        )
        return ConflictAdvance(
            action="merged" if outcome.merged else outcome.action,
            reason=outcome.reason if not outcome.merged else reason,
            decision=decision,
            new_head=new_head,
            diffs=diffs,
            files=[{"path": path, "markdown": text} for path, text in published.items()],
            merge=outcome,
            asked_again=False,
            review=rerun.as_dict(),
        )
    return ConflictAdvance(
        action=rerun.action,
        reason=rerun.reason,
        decision=decision,
        new_head=new_head,
        diffs=diffs,
        asked_again=False,
        review=rerun.as_dict(),
    )
