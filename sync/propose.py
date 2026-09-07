#!/usr/bin/env python3
"""Upward path: propose a fork's exported entries into this repo.

    # preview only (nothing written)
    python3 sync/propose.py --export fork-export.yaml

    # write the resulting corpus files into a checkout, no git
    python3 sync/propose.py --export fork-export.yaml --apply

    # branch + commit + push + `gh pr create`, against a fresh origin/main
    python3 sync/propose.py --export fork-export.yaml --open-pr

Behaviour is decided entirely by sync/plan.py; this module renders a plan into
file changes and (optionally) a pull request:

- per entry, never per file: changed documents are re-emitted with the touched
  entries upserted by uuid and entries sorted by uuid, so two proposals for the
  same identity collide on the same lines in git instead of landing twice;
- idempotent: a plan whose every item is `no-op` produces no branch and no PR;
- new entries land in corpus/unverified/ only; a revision that targets an entry
  in corpus/verified/ is reported as a conflict and is never written;
- duplicate candidates are reported and not written unless
  `--allow-duplicate-candidates` is given (they then land as ordinary `new`
  entries so that humans can merge them with lifecycle.supersedes);
- nothing here can remove an entry (see _common.write_documents).

The GitHub call is isolated in `open_pull_request`; everything before it is
pure computation over the working tree and is exercised offline by
tests/test_sync_propose.py.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EXIT_ERROR,
    EXIT_GATE,
    EXIT_OK,
    Corpus,
    GateResult,
    Runner,
    SyncError,
    default_runner,
    gates_summary,
    json_dumps,
    load_corpus,
    load_export,
    run_source_gates,
    upsert_entry,
    write_documents,
)
from plan import (  # noqa: E402
    Plan,
    add_common_arguments,
    compute_plan,
    render_plan,
    resolve_dirs,
)


@dataclasses.dataclass
class Proposal:
    plan: Plan
    changes: dict[pathlib.Path, dict]  # path -> full document to write
    applied: list[str]  # uuids written
    proposal_id: str
    origin: str

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def display_path(self, path: pathlib.Path) -> str:
        try:
            return str(path.resolve().relative_to(pathlib.Path(self.plan.corpus_root).resolve().parent))
        except ValueError:
            return str(path)

    def branch_name(self) -> str:
        origin = re.sub(r"[^a-z0-9._-]+", "-", self.origin.lower()).strip("-") or "unknown-origin"
        return f"sync/{origin}/{self.proposal_id}"

    def to_public(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "origin": self.origin,
            "branch": self.branch_name(),
            "applied": sorted(self.applied),
            "changed_files": sorted(self.display_path(p) for p in self.changes),
            "plan": self.plan.to_public(),
        }


def build_proposal(
    plan: Plan, corpus: Corpus, *, allow_duplicate_candidates: bool = False
) -> Proposal:
    """Turn a plan into per-file document changes. Only `new` and `revision`
    items are applied (plus duplicate candidates when explicitly allowed)."""
    changes: dict[pathlib.Path, dict] = {}
    applied: list[str] = []
    items = list(plan.applicable())
    if allow_duplicate_candidates:
        items += plan.by_action("duplicate-candidate")

    for item in items:
        assert item.entry_after is not None and item.target_path is not None
        path = corpus.root.resolve().parent / item.target_path
        # Fail closed on the layer rule even if a plan item was hand-edited.
        unverified_root = (corpus.root / "unverified").resolve()
        if not path.resolve().is_relative_to(unverified_root):
            raise SyncError(f"refusing to write outside corpus/unverified/: {path}")
        if item.action == "revision" and item.current_layer != "unverified":
            raise SyncError(f"refusing to write a revision of a {item.current_layer} entry: {path}")
        if path not in changes:
            existing = corpus.documents.get(path)
            if existing:
                changes[path] = copy.deepcopy(existing.data)
            else:
                changes[path] = {
                    "schema_version": 2,
                    "kind": item.kind,
                    "layer": "unverified",
                    "updated_at": plan.today,
                    "entries": [],
                }
        doc = changes[path]
        upsert_entry(doc, item.entry_after)
        doc["updated_at"] = plan.today
        applied.append(item.uuid)

    digest = hashlib.sha256()
    for item in sorted(items, key=lambda i: i.uuid):
        digest.update(f"{item.uuid}:{item.proposed_hash}\n".encode())
    origin = plan.origin_repos[0] if plan.origin_repos else "unknown-origin"
    return Proposal(
        plan=plan,
        changes=changes,
        applied=applied,
        proposal_id=digest.hexdigest()[:12],
        origin=origin,
    )


def apply_proposal(proposal: Proposal, corpus: Corpus, *, dry_run: bool = False) -> list[pathlib.Path]:
    return write_documents(corpus, proposal.changes, dry_run=dry_run)


def run_result_gates(tools_dir: pathlib.Path, paths: list[pathlib.Path], *, runner: Runner = default_runner) -> list[GateResult]:
    """Re-validate the documents a proposal wrote, not just the export."""
    if not paths:
        return []
    return run_source_gates(tools_dir, paths, runner=runner)


# --------------------------------------------------------------------------
# rendering the PR


def render_pr_title(proposal: Proposal) -> str:
    counts = proposal.plan.counts()
    parts = []
    if counts["new"]:
        parts.append(f"{counts['new']} new")
    if counts["revision"]:
        parts.append(f"{counts['revision']} revised")
    what = ", ".join(parts) or "no changes"
    return f"knowledge: {what} from {proposal.origin}"


def render_pr_body(proposal: Proposal, *, central_collection: bool = False) -> str:
    plan = proposal.plan
    if central_collection:
        scan = (
            "Every entry below was validated and redaction-checked by a trusted "
            "central collection scan of already-public GitHub blobs. A public blob "
            "proves where bytes were read, not that a source-side scan occurred, "
            "and not contributor identity or technical truth."
        )
    else:
        scan = (
            "Every entry below was validated and redaction-checked in the fork before "
            "export."
        )
    lines = [
        f"Proposal `{proposal.proposal_id}` from `{proposal.origin}` — generated by `sync/propose.py`.",
        "",
        scan,
        "New entries land in `corpus/unverified/`; nothing here touches",
        "`corpus/verified/`.",
        "",
        "| action | uuid | slug | reason |",
        "|---|---|---|---|",
    ]
    for item in plan.items:
        reason = item.reason.replace("|", "\\|")
        lines.append(f"| `{item.action}` | `{item.uuid}` | `{item.slug}` | {reason} |")
    unapplied = plan.by_action("conflict") + [
        i for i in plan.by_action("duplicate-candidate") if i.uuid not in proposal.applied
    ]
    if unapplied:
        lines += ["", "### Not applied — needs a human", ""]
        for item in unapplied:
            lines.append(f"- `{item.action}` `{item.uuid}` (`{item.slug}`): {item.reason}")
            for rel in item.related:
                lines.append(f"  - related: `{rel}`")
    notes = [(i, n) for i in plan.items for n in i.notes if i.uuid in proposal.applied]
    if notes:
        lines += ["", "### Normalisations applied", ""]
        for item, note in notes:
            lines.append(f"- `{item.uuid}`: {note}")
    lines += ["", "Files:", ""]
    for p in sorted(proposal.changes):
        lines.append(f"- `{proposal.display_path(p)}`")
    return "\n".join(lines) + "\n"


def render_commit_message(proposal: Proposal) -> str:
    title = f"feat(corpus): {render_pr_title(proposal)[len('knowledge: '):]}"
    body = [
        "",
        f"Proposal {proposal.proposal_id} generated by sync/propose.py from",
        f"{proposal.origin}. Entries are upserted by uuid into",
        "corpus/unverified/; the review-gated promotion to corpus/verified/",
        "is a separate step.",
        "",
    ]
    for item in proposal.plan.items:
        if item.uuid in proposal.applied:
            body.append(f"- {item.action}: {item.uuid} {item.slug}")
    return title + "\n" + "\n".join(body) + "\n"


# --------------------------------------------------------------------------
# the GitHub call, isolated


@dataclasses.dataclass
class PullRequestResult:
    status: str  # created | exists | nothing-to-propose | failed
    branch: str | None = None
    url: str | None = None
    detail: str = ""


def _run(runner: Runner, cmd: list[str], cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    proc = runner(cmd, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise SyncError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}{proc.stderr}")
    return proc


def existing_pull_request(runner: Runner, repo: pathlib.Path, branch: str) -> str | None:
    proc = runner(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
        cwd=str(repo),
    )
    if proc.returncode != 0:
        raise SyncError(f"gh pr list failed: {proc.stdout}{proc.stderr}")
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SyncError(f"gh pr list returned non-JSON: {proc.stdout!r}") from exc
    return data[0]["url"] if data else None


def open_pull_request(
    exports: list[pathlib.Path],
    *,
    repo: pathlib.Path,
    tools_dir: pathlib.Path | None,
    remote: str,
    base: str,
    day: str | None,
    allow_duplicate_candidates: bool,
    runner: Runner = default_runner,
    log=print,
    central_collection: bool = False,
) -> PullRequestResult:
    """Plan against a fresh `<remote>/<base>` in a temporary worktree, commit
    the resulting documents, push (never force) and open a PR.

    Planning happens against the freshly fetched base, not the caller's
    working tree, so that two forks racing to propose the same uuid see each
    other's merged result: the second plan degrades to a revision or no-op.
    """
    _run(runner, ["git", "fetch", remote, base], cwd=repo)
    worktree = pathlib.Path(tempfile.mkdtemp(prefix="vaws-sync-")).resolve()
    try:
        _run(runner, ["git", "worktree", "add", "--detach", str(worktree), f"{remote}/{base}"], cwd=repo)
        corpus = load_corpus(worktree / "corpus")
        tools = tools_dir if tools_dir else worktree / "tools"
        gates = run_source_gates(tools, exports, runner=runner)
        if not all(g.ok for g in gates):
            return PullRequestResult(status="failed", detail=gates_summary(gates))
        plan = compute_plan([load_export(p) for p in exports], corpus, day=day)
        proposal = build_proposal(plan, corpus, allow_duplicate_candidates=allow_duplicate_candidates)
        log(render_plan(plan))
        if proposal.is_empty:
            return PullRequestResult(status="nothing-to-propose", detail="every entry is a no-op or unapplied")

        written = apply_proposal(proposal, corpus)
        result_gates = run_result_gates(tools, written, runner=runner)
        if not all(g.ok for g in result_gates):
            return PullRequestResult(status="failed", detail=gates_summary(result_gates))

        branch = proposal.branch_name()
        url = existing_pull_request(runner, repo, branch)
        if url:
            return PullRequestResult(status="exists", branch=branch, url=url,
                                     detail="an open PR for this exact proposal already exists")

        _run(runner, ["git", "checkout", "-b", branch], cwd=worktree)
        _run(runner, ["git", "add", "--", *[str(p.relative_to(worktree)) for p in written]], cwd=worktree)
        # Commit message and PR body live outside the worktree so they are
        # never staged, and vanish with the context manager.
        with tempfile.TemporaryDirectory(prefix="vaws-sync-aux-") as aux:
            msg_path = pathlib.Path(aux) / "commit-message.txt"
            msg_path.write_text(render_commit_message(proposal), encoding="utf-8")
            _run(runner, ["git", "commit", "--quiet", "-F", str(msg_path)], cwd=worktree)
            _run(runner, ["git", "push", remote, f"{branch}:{branch}"], cwd=worktree)  # never --force

            body_path = pathlib.Path(aux) / "pr-body.md"
            body_path.write_text(
                render_pr_body(proposal, central_collection=central_collection),
                encoding="utf-8",
            )
            proc = _run(
                runner,
                ["gh", "pr", "create", "--base", base, "--head", branch,
                 "--title", render_pr_title(proposal), "--body-file", str(body_path)],
                cwd=repo,
            )
        return PullRequestResult(status="created", branch=branch, url=(proc.stdout or "").strip())
    finally:
        runner(["git", "worktree", "remove", str(worktree)], cwd=str(repo))


# --------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser)
    parser.add_argument("--json", type=pathlib.Path, default=None, help="write the proposal summary as JSON here")
    parser.add_argument("--apply", action="store_true", help="write the changed documents into --corpus (no git)")
    parser.add_argument("--open-pr", action="store_true", help="branch, commit, push and open a PR with gh")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--allow-duplicate-candidates", action="store_true",
                        help="land duplicate candidates as new entries anyway (still reported)")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero without writing anything if any entry is a conflict or duplicate candidate")
    args = parser.parse_args(argv)

    try:
        repo, corpus_dir, tools_dir = resolve_dirs(args)
        if args.open_pr:
            result = open_pull_request(
                args.export, repo=repo, tools_dir=(pathlib.Path(args.tools_dir).resolve() if args.tools_dir else None),
                remote=args.remote, base=args.base, day=args.today,
                allow_duplicate_candidates=args.allow_duplicate_candidates,
            )
            print(f"{result.status}" + (f": {result.url}" if result.url else "") + (f"\n{result.detail}" if result.detail else ""))
            return EXIT_OK if result.status != "failed" else EXIT_GATE

        exports = [load_export(p) for p in args.export]
        corpus = load_corpus(corpus_dir)
        gates = run_source_gates(tools_dir, args.export)
        print(gates_summary(gates))
        if not all(g.ok for g in gates):
            print("refusing to propose: source gates did not pass", file=sys.stderr)
            return EXIT_GATE
        plan = compute_plan(exports, corpus, day=args.today)
        proposal = build_proposal(plan, corpus, allow_duplicate_candidates=args.allow_duplicate_candidates)
        print()
        print(render_plan(plan))
        blocking = plan.by_action("conflict") + plan.by_action("duplicate-candidate")
        if args.strict and blocking:
            print(f"--strict: {len(blocking)} entries need a human; nothing written", file=sys.stderr)
            return EXIT_ERROR
        if args.json:
            args.json.write_text(json_dumps(proposal.to_public()), encoding="utf-8")
        if proposal.is_empty:
            print("\nnothing to propose")
            return EXIT_OK
        if args.apply:
            written = apply_proposal(proposal, corpus)
            result_gates = run_result_gates(tools_dir, written)
            print(gates_summary(result_gates))
            if not all(g.ok for g in result_gates):
                print("written documents did not pass the gates; do not commit them", file=sys.stderr)
                return EXIT_GATE
            for p in written:
                print(f"wrote {p}")
        else:
            print(f"\nwould write {len(proposal.changes)} file(s); pass --apply or --open-pr")
        return EXIT_OK
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
