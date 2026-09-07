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

The publisher posts a single comment and edits it in place. That depends on the
rendering being deterministic, which is pinned by a test: if the same corpus
rendered differently between runs, every run would produce a spurious edit and
reviewers would learn to ignore the comment.

The unprivileged gate workflow and the trusted publisher are separate workflows
with distinct names. GitHub will not run a `workflow_run` listener that names
itself; a local YAML parser accepting that is not GitHub validity. Incoming
pull request code runs the gates with `contents: read`, no write token, and
`persist-credentials: false`. It never receives the comment token.

The publisher listens for completion of `Review gates`, checks out the trusted
default-branch revision that contains it (`github.sha`), and never checks out
the pull request head, never installs the pull request's dependencies, and
never executes artifact files as code. Cross-run artifact download needs
`actions: read`; posting the comment needs `pull-requests: write`. Those are
the only extra permissions.

The pull request number inside the artifact is not authority. The publisher
derives the associated pull request from the `workflow_run` event and the
GitHub API, then refuses to write unless that pull request's current head
matches the source revision, in this repository. The commit-associated PR
endpoint may name the pull request that contains the run's commit; its
`.head.sha` is the live current head and is not run-head evidence. A fallback
write is allowed only when that current head still equals an immutable SHA
from the triggering run or event. Live-head equality with itself, ancestry,
branch name, or artifact fields cannot stand in for that linkage. A marker in
a human comment is not ownership: only a `github-actions[bot]` comment for
this report is updated. An old run does not overwrite a newer head's report.
Comment listing walks every page; the first marker match is not enough.

The artifact body is data. The publisher validates `gate-results.json` and
re-renders the comment from that data with the trusted helper, binding the
visible text to the source run and head. A missing, malformed, or inconsistent
report produces no comment at all — in particular it cannot produce a success
comment. The marker, the filename, and a well-formed looking markdown file are
not proof of authenticity.

A `workflow_run` listener is taken from the default branch. Until that
revision is on the default branch, live scheduling cannot be demonstrated by
this repository's tests. A green local suite does not mean GitHub ran the
publisher.

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

## Optional advisory Grok adapter

`bot/triage_grok.py` is an optional, call-capable helper. It is not part of the
deterministic gate verdict and is not invoked by the pull-request workflow.

It freezes the selected input bytes first, then runs the existing `load`,
`schema`, and `redaction` gates against that snapshot, and only then may call
a configured xAI Chat Completions endpoint. The snapshot mapping keeps the
physical `corpus/verified` and `corpus/unverified` path context the schema
gate already uses, and directory freeze includes the YAML/YML/JSON files those
CLIs discover; provider entry discovery remains the bot's YAML set. The
advisory artifact binds the snapshot bytes as well as the selected
UUID/`content_hash` set. `XAI_API_KEY`
and `XAI_MODEL` must be explicitly present in the environment; this repository
does not ship them. Missing configuration, a failed mandatory gate, no eligible
input, or a provider/parse failure is `unavailable` / `error`, never a
successful semantic review. Corpus prose is sent as untrusted data. The adapter
does not register tools.

Output is a separate advisory JSON artifact. A successful empty `candidates`
list is distinct from `unavailable` / `error`. `--asserted-out` writes the
existing `--asserted` pair format for an explicit later handoff; the adapter
does not feed itself into `bot/report.py`, does not mutate gate JSON, and
cannot mark an entry verified. Offline fixture tests do not establish live
Grok quality or a deployed GitHub review.

```
python3 bot/triage_grok.py \
  --source-repo owner/vaws-knowledge \
  --source-ref <commit> \
  --json advisory.json \
  --asserted-out asserted.json \
  corpus examples

python3 bot/report.py --mode pr corpus examples --asserted asserted.json
```

The second command is an explicit handoff. Gate results remain authoritative.

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
