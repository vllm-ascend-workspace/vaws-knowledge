# Review pipeline

## What the pipeline decides, and what it must not

The deterministic gates can block a proposal on: schema conformance and
`content_hash` integrity, a redaction re-scan under the current ruleset, exact
and near duplicates, coordinate conflicts, and identifier integrity.

They cannot decide whether a claim is **true**.

So passing every gate permits `corpus/unverified/` and nothing more. The report
says so in its own `permits` field rather than leaving a reader to infer it from
a green check, and `tests/test_bot_gates.py` asserts it against a run where
every gate passes — because this is the rule most likely to be eroded by one
convenience commit, and the result of eroding it is a machine that publishes
unreviewed claims with an endorsement attached.

Reaching `corpus/verified/` additionally needs a followable evidence reference
and a confirmation from somebody who is not the submitter. No automation is
involved in that step.

## Gate order

| Gate | Blocking | What it establishes |
|---|---|---|
| `load` | yes | the documents parse and are addressable |
| `schema` | yes | conformance to `knowledge-v2`, plus `content_hash` recomputation |
| `redaction` | yes | no leak under the **current** ruleset, not the one that cleared the export |
| `integrity` | yes | `uuid` uniqueness and identifier shape across the corpus |
| `duplicates` | yes | exact and near duplicates reported — never merged |
| `conflicts` | yes | coordinate diffs and undeclared dimensions recorded on both entries |
| `staleness` | pull request: no · audit: yes | entries past the re-verification horizon |

`load` runs first because every later gate is meaningless on documents that did
not parse, and reporting six failures for one broken file trains people to skim.

`staleness` is advisory on a proposal and blocking in the audit. An entry going
stale is the passage of time, not something the author of an unrelated pull
request did, and blocking their work on it would make the sweep something to
route around.

## Fail closed

A gate whose tool is missing, or which errors, is a **failure**. Never a pass,
never a skip.

"We could not check" and "we checked and it was fine" are the two states a
review pipeline must never confuse, and the confusion has a specific shape here:
`tools/`, `sync/` and `conformance/` are separate packages, so a checkout can
legitimately be missing any of them. A pipeline that skipped an absent gate
would go green on a checkout where nothing was verified at all.

The workflow enforces the same rule one level up: an empty report is treated as
a failure rather than as "nothing to say".

There is no separate canonical-hash gate. An earlier draft had one that invoked
`tools/canonical.py --check`, a flag that tool has never had — the draft was
written against an assumed interface while `tools/` was built in parallel, so
the gate could not have passed on any proposal. It was also redundant, since
`tools/validate.py` already recomputes `content_hash`. Removing it was cheaper
than growing a tool's surface to serve a duplicate check.

## One comment, updated

The pull request workflow posts a single comment and edits it in place. That
depends on the rendering being deterministic, which is pinned by a test: if the
same corpus rendered differently between runs, every run would produce a
spurious edit and reviewers would learn to ignore the comment.

The workflow is split into two jobs. The gates run untrusted pull request code
with read-only permissions and no secrets; a separate job runs from the trusted
base with only `pull-requests: write` and consumes the artifact. One job holding
both would hand a fork's branch a token that can comment as the repository.

## Where a triage step plugs in, and what it may not do

Conflict detection needs to notice that two entries are *about the same
phenomenon* and *say opposite things*, which is a judgement about text. A
language-model triage step can help there, and `bot/conflicts.py` accepts
asserted pairs from one.

The boundary is deliberate and narrow. An assertion says only "these two look
contradictory". Everything that follows — the coordinate diff, the derived
undeclared dimensions, the decision to block — stays in deterministic code, and
so does the verdict. No model is named anywhere in the gate logic; the triage
step is a workflow step that can be swapped or removed without changing what any
gate concludes.

The reason is the same as the rule at the top of this document. A model can be
useful at spotting candidates and cannot be trusted to establish truth, so it is
wired where being wrong produces a false candidate for a human to dismiss,
rather than a false verdict for a human to believe.

## Policy is tracked, not configured in CI

The staleness horizon and the supported-version window live in `bot/policy.yaml`
and are reviewed like code. They change what the pipeline blocks, and a
threshold tuned in a CI variable is a behaviour change nobody reviewed.

Nothing in that file is knowledge. It configures how the corpus is judged, never
what is true about Ascend.

## Branch protection is not in this repository

`CODEOWNERS` requests reviewers; it does not require them. Requiring review on
`corpus/verified/` and `schemas/` needs branch protection with "Require review
from Code Owners", which is a repository-admin setting and cannot be committed.
Until an administrator enables it, the ownership file is advisory — worth knowing
before treating a merge into `verified/` as having been reviewed.
