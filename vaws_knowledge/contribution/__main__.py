"""``python -m vaws_knowledge.contribution`` — local contribution helpers.

Network writes are opt-in through injected credentials. This entrypoint is
for the knowledge package; the root CLI is not modified in this delivery.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from vaws_knowledge.contribution.ci import maybe_merge_from_ci, run_trusted_ci
from vaws_knowledge.contribution.conflict import HumanDecision, advance_conflict
from vaws_knowledge.contribution.github import UrllibContributionGitHub
from vaws_knowledge.contribution.grok import GrokClassifier
from vaws_knowledge.contribution.merge import FileSerializer, merge_reviewed
from vaws_knowledge.contribution.recall import production_recall
from vaws_knowledge.contribution.review import ReviewResult, review_candidate
from vaws_knowledge.contribution.submit import SubmitConfig, after_capture, prepare_candidate, submit_pending


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _print(payload: dict) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    record = prepare_candidate(
        Path(args.candidate),
        state_root=Path(args.state_root),
        public_root=Path(args.public_root),
    )
    return _print(record.to_dict())


def _cmd_submit(args: argparse.Namespace) -> int:
    from vaws_knowledge.contribution.pending import iter_pending

    records = [record for record in iter_pending(Path(args.state_root))
               if record.status in {"pending", "awaiting_transport"}]
    if not records:
        return _print({"status": "unchanged", "pending": []})
    if not args.git_repo:
        return _print({"status": "awaiting_transport", "reason": "a configured fork checkout is required"})
    from vaws_knowledge.github_transport import github_token

    try:
        token = github_token()
    except (OSError, RuntimeError) as exc:
        return _print({"status": "awaiting_transport", "reason": str(exc)})
    github = UrllibContributionGitHub(token)
    config = SubmitConfig(upstream=args.upstream, fork=args.fork or args.upstream, push_remote="origin")
    updated = submit_pending(
        records[0],
        state_root=Path(args.state_root),
        public_root=Path(args.public_root),
        git_repo=Path(args.git_repo),
        github=github,
        config=config,
    )
    return _print(updated.to_dict())


def _cmd_review(args: argparse.Namespace) -> int:
    text = Path(args.markdown).read_text(encoding="utf-8")
    recall = production_recall(
        base_sha=args.base,
        environ=os.environ,
        url=args.openviking_url,
        corpus_sha=args.corpus_sha,
    )
    result = review_candidate(
        text,
        candidate_head=args.head,
        base_sha=args.base,
        recall=recall,
        classifier=GrokClassifier(environ=os.environ),
    )
    if args.json:
        Path(args.json).write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return _print(result.as_dict())


def _cmd_merge(args: argparse.Namespace) -> int:
    payload = _load_json(Path(args.review))
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("missing GITHUB_TOKEN; merge not attempted", file=sys.stderr)
        return 2
    github = UrllibContributionGitHub(token)
    review = ReviewResult.from_dict(payload)
    outcome = merge_reviewed(
        review,
        github=github,
        repository=args.repo or str(payload.get("repository") or ""),
        pr_number=args.pr or int(payload.get("pr_number") or 0),
        serializer=FileSerializer(Path(args.state_root)) if args.state_root else None,
    )
    return _print(outcome.as_dict())


def _cmd_ci(args: argparse.Namespace) -> int:
    event = _load_json(Path(args.event or os.environ.get("GITHUB_EVENT_PATH") or ""))
    repository = args.repo or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("missing GITHUB_TOKEN; trusted CI cannot fetch PR data", file=sys.stderr)
        return 2
    github = UrllibContributionGitHub(token)
    payload = run_trusted_ci(
        event,
        github=github,
        repository=repository,
        stash=Path(args.stash),
        classifier=GrokClassifier(environ=os.environ),
        checkout_ref=args.checkout_ref,
        default_branch=args.default_branch,
        environ=os.environ,
        openviking_url=args.openviking_url,
    )
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.merge:
        maybe_merge_from_ci(payload, github=github)
    return _print(payload)


def _cmd_resolve(args: argparse.Namespace) -> int:
    payload = _load_json(Path(args.review))
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("missing GITHUB_TOKEN; resolve not attempted", file=sys.stderr)
        return 2
    github = UrllibContributionGitHub(token)
    previous = None
    if args.decision_json:
        previous = HumanDecision.from_dict(_load_json(Path(args.decision_json)))
    text = Path(args.markdown).read_text(encoding="utf-8")
    review = ReviewResult.from_dict(payload)
    related = []
    for item in payload.get("recall", {}).get("documents") or []:
        from vaws_knowledge.contribution.documents import ContentIdentity
        from vaws_knowledge.contribution.recall import RelatedDocument

        if isinstance(item, dict) and item.get("path") and item.get("git_sha"):
            related.append(
                RelatedDocument(
                    identity=ContentIdentity(path=str(item["path"]), git_sha=str(item["git_sha"])),
                    title=str(item.get("title") or ""),
                    body="",
                )
            )
    result = advance_conflict(
        review,
        github=github,
        repository=args.repo,
        pr_number=args.pr,
        candidate_text=text,
        related=related,
        recall=production_recall(base_sha=review.base_sha, environ=os.environ),
        classifier=GrokClassifier(environ=os.environ),
        git_repo=Path(args.git_repo) if args.git_repo else None,
        previous_decision=previous,
    )
    return _print(result.as_dict())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vaws_knowledge.contribution")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="redact a public copy and write a pending record")
    prepare.add_argument("--candidate", required=True)
    prepare.add_argument("--state-root", required=True)
    prepare.add_argument("--public-root", required=True)
    prepare.set_defaults(func=_cmd_prepare)

    submit = sub.add_parser("submit", help="submit pending public copies (needs transport)")
    submit.add_argument("--state-root", required=True)
    submit.add_argument("--public-root", required=True)
    submit.add_argument("--git-repo")
    submit.add_argument("--candidate")
    submit.add_argument("--upstream", default="owner/vaws-knowledge-corpus")
    submit.add_argument("--fork")
    submit.set_defaults(func=_cmd_submit)

    review = sub.add_parser("review", help="review one markdown document")
    review.add_argument("--markdown", required=True)
    review.add_argument("--head", required=True)
    review.add_argument("--base", required=True)
    review.add_argument("--json")
    review.add_argument("--corpus-sha")
    review.add_argument("--openviking-url")
    review.set_defaults(func=_cmd_review)

    merge = sub.add_parser("merge", help="conditionally merge a bound review")
    merge.add_argument("--review", required=True)
    merge.add_argument("--repo")
    merge.add_argument("--pr", type=int)
    merge.add_argument("--state-root")
    merge.set_defaults(func=_cmd_merge)

    ci = sub.add_parser("ci", help="trusted CI: PR markdown as data")
    ci.add_argument("--event")
    ci.add_argument("--repo")
    ci.add_argument("--stash", default="contribution-stash")
    ci.add_argument("--checkout-ref", default="${{ github.event.repository.default_branch }}")
    ci.add_argument("--default-branch", default="main")
    ci.add_argument("--json")
    ci.add_argument("--merge", action="store_true")
    ci.add_argument("--openviking-url")
    ci.set_defaults(func=_cmd_ci)

    resolve = sub.add_parser("resolve", help="advance a conflict: human comment → rewrite → re-review → merge")
    resolve.add_argument("--review", required=True)
    resolve.add_argument("--markdown", required=True)
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--pr", type=int, required=True)
    resolve.add_argument("--git-repo")
    resolve.add_argument("--decision-json", help="previously recorded human decision to reuse")
    resolve.set_defaults(func=_cmd_resolve)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
