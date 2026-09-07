# Knowledge operations summary (Stage 2)

This file distinguishes what this revision *implements* from what has *not*
been executed live. Root owns review, publication, any repository-setting
adjustment, and a bounded live run after acceptance.

Checked on 2026-09-07 against public knowledge main
`44dd33b96d0ca6b6c0e6aea52cc5f66fd35014a1`.

## Code implemented in this tree

| Area | What landed |
|---|---|
| A. Central collection | `sync/collect.py`, `.github/workflows/collect.yml` (`Central collection`) |
| B. Advisory review | `bot/advisory_review.py`, `.github/workflows/advisory-review.yml` (`Advisory review`) |
| C. Verified snapshot | `sync/snapshot.py`, `.github/workflows/publish-snapshot.yml` (`Verified snapshot`) |
| Consumption | `docs/periodic-pull-template.yml` (prints Phase B import CLI; does not refresh) |
| Reuse | existing `tools/export.py`, gates, `sync/plan.py`, `sync/propose.py --open-pr`, `bot/triage_grok.py`, `sync/publish.py`, `bot/publish_comment.py` association |

Workflows request an explicit finite cadence, per-repo concurrency, job
timeouts, and bounded fork/file/byte/page counts. Preview is read-only.
Propose uses job-level `contents: write` and `pull-requests: write` only.
Read/fetch/model jobs stay `contents: read`. Settings are not changed here.

## Offline fixture acceptance

Deterministic tests in this revision exercise, with injected GitHub/HTTP/`gh`
fakes and **zero** live network:

- renamed parent resolved by numeric id `1196723340`
- wrong fork ancestry
- multi-page complete and limited discovery
- immutable commit/blob binding
- executable / symlink / path traversal / oversize rejection
- v1 and incomplete scope as unsupported
- valid fully bounded v2 candidate
- schema/redaction failure with zero model and PR calls
- duplicate uuid/hash across forks
- conflict / verified-revision refusal
- repeat-run does not open a second proposal
- failed fetch/gate preserves prior corpus state
- spoofed / stale PR association
- missing Grok configuration → unavailable, zero provider calls
- model advisory cannot approve or promote
- verified-only publish with manifest verification
- workflow names, triggers, permissions and checkout boundaries
- successful-export-only handoff via isolated current-success directory and manifest
- proposal mode reports the existing proposer's fresh-main plan, not a stale local preview
- incompatible same-uuid/hash metadata reported, not coalesced
- public retrieval bindings and planner conflict outcomes
- advisory selected-blob failure is unavailable with zero provider calls
- advisory comment requires matching artifact binding
- Phase B `knowledge_shared_cache.py import` recipe

That is offline acceptance, not a live GitHub or xAI run.

## Workflow installed / default-branch active

| Workflow | Path | In this revision | Default-branch active live |
|---|---|---|---|
| Review gates | `.github/workflows/pr-review.yml` | yes (pre-existing) | only after this history is on `main` |
| Review comment | `.github/workflows/pr-review-comment.yml` | yes (pre-existing) | only after this history is on `main` |
| Corpus audit | `.github/workflows/corpus-audit.yml` | yes (pre-existing) | only after this history is on `main` |
| Central collection | `.github/workflows/collect.yml` | yes | **not** until root publishes this revision to the default branch |
| Advisory review | `.github/workflows/advisory-review.yml` | yes | **not** until that publication; `workflow_run` listeners run default-branch code |
| Verified snapshot | `.github/workflows/publish-snapshot.yml` | yes | **not** until that publication |

A green local suite does not mean GitHub scheduled or dispatched these jobs.

## Actual scheduled / manual executions

None in this implementation turn. No `workflow_dispatch`, no waiting on a
cron, no GitHub API mutation.

## Candidate PR creation

Code path exists (`sync/collect.py --mode propose` → `sync/propose.py
--open-pr`). No candidate PR was opened. Preview cannot open one.

Repository setting observed earlier:
`default_workflow_permissions=read`,
`can_approve_pull_request_reviews=false`. This task does not change that.
Root may grant the built-in token permission to create PRs after review.

## CI approval / execution

With the built-in `GITHUB_TOKEN`, pull-request CI for PRs this workflow
opens is **awaiting any GitHub-required approval**, not executed or passed.
This implementation does not auto-approve reviews or alter branch
protection.

## Real Grok response

`bot/triage_grok.py` is call-capable. No `XAI_API_KEY` / `XAI_MODEL` is
configured in the repository-level name listing. Missing configuration
produces `unavailable` and zero provider calls. No live model request was
sent. Offline fixtures are not Grok quality evidence.

## Actual consumer refresh

None. The snapshot publisher writes an artifact in Actions after gates; it
cannot populate another developer's untracked
`.vaws-local/knowledge/shared/`. The periodic-pull template (`docs/periodic-pull-template.yml`) is
documentation and is not installed on forks.

## Honest no-op

The current public scaffold knowledge (two migrated entries with unresolved
dimensions; nine v1 model facts) is expected to be unsupported/blocked.
Zero eligible entries is a successful no-op, not a failed implementation.
