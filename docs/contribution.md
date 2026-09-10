# Knowledge contribution and trusted review

This package turns a local Markdown candidate into a public copy, an
idempotent fork PR, a Grok review against already-published related
documents, and a merge that is bound to the candidate head and the base
SHA. It is a library with `python -m vaws_knowledge.contribution`. The
root CLI, package dependencies, and live GitHub workflows are wired by
the integrator.

Public review records responsibility for published text. It does not prove
hardware facts, and Grok does not reproduce hardware measurements. Local
experience and public knowledge are both reference, not axioms. Retrieval
comparison uses relevance and known conditions, not a trust rank.

## What an author supplies

Title and non-empty body. Optional conditions, evidence, and source stay
in the prose when known. There is no required frontmatter, UUID, type
enum, or coordinate block.

```markdown
# 一次图模式启动失败的排查经验

当时遇到了……，检查后发现……，采用……后启动成功。
只在当时环境验证过，其他版本尚未确认。
```

Ordinary observations may publish with that reporter scope. Strong metrics
or universal conclusions must be commensurate with the evidence in the
text.

## Local prepare and submit

`after_capture` / `prepare_candidate` leave the candidate file unchanged,
write a redacted public copy, and record a digest-keyed pending JSON under
a caller-supplied state root. That store is not a generic task queue.

Retrying the same public content reuses the pending record, branch, and
PR. Offline or GitHub authentication failure leaves `awaiting_transport`
and does not fail capture.

Content digest (`sha256:…`) is only for idempotency and integrity. Path +
Git SHA identify published content. A digest must not be passed off as a
Git identity.

## Review decisions

Trusted review recalls related documents from the already-published
library with native OpenViking `find` (injectable client), then classifies:

| Decision | Default action |
|---|---|
| `new` | merge when evidence policy allows |
| `duplicate` | close; do not re-enter |
| `supplement` | auto-write a reviewable Markdown diff, re-check the new head, merge |
| `condition_difference` | keep both; merge |
| `conflict` | ask a human for direction, then Grok rewrites, re-checks, merges |
| `insufficient_evidence` | hold this contribution only |

External unavailability (recall, Grok, GitHub permission) is never a pass.

The result binds `candidate_head`, `base_sha`, and related `{path, git_sha}`.
A changed PR head is re-reviewed. An advanced base is re-checked for
related duplicates and conflicts. Two PRs that both passed against an old
base cannot both land: merge re-reads the live default-branch SHA, holds a
per-base lock, and sends GitHub merge with `sha=` of the bound head.

## Human direction on true conflicts

Grok may rewrite published Markdown. When the claims cannot be auto-resolved,
it posts a short PR comment (divergence, already-published excerpts,
suggested options). A person with write/maintain/admin on the repository
replies in ordinary language — no form.

Accepted replies are GitHub issue comments, review comments, or review
bodies that target that conflict binding (`in_reply_to` or the conflict
key). Candidate Markdown, bot comments, and users without write permission
are not authorization.

Directions:

- keep the published text → close the PR
- prefer the candidate → rewrite the published file
- combine / rewrite → merge the texts into the published file

Grok then produces a redacted Markdown diff, commits a new head, re-reviews
technical results, and merges through the same CAS path. The expected
rewrite head does **not** ask for the same decision again. A new decision
is required only when the conflict fingerprint changes (candidate digest +
related path and **content** digest). An unrelated base advance that does
not change those contents is re-checked and reuses the decision.

Waiting for a reply blocks only that public contribution.

## Trusted CI template

`examples/corpus-contribution/trusted-review.yml.tmpl` is a template. The
`.tmpl` suffix keeps it out of this package's YAML load/schema/redaction
gates. It is not enabled here.

Activation still requires the integrator to: copy it to the knowledge-content
repository as a real workflow, drop the suffix, replace every `<pin>` with a
reviewed action and `vaws-knowledge` version, configure OpenViking
(`OPENVIKING_URL`) and xAI credentials, and enable the workflow on the
default branch. Until those pins exist, live GitHub Actions is unverified.

The secret-bearing job checks out the repository default branch, never the
PR head, never installs or executes fork code, and reviews the immutable
base→head change set. Unchanged README files are not selected as the
contribution. Every knowledge Markdown change is classified. Unsupported
paths (workflows, installable files, root README edits) refuse automatic
merge. Missing, truncated, or unavailable recall/diff evidence does not
permit publish. Permission and provider errors are explicit.

## Join points (Grok 1 / integrator)

- Capture writes `# Title` plus body Markdown. This module reads that shape
  and does not depend on uncommitted `vaws_knowledge.markdown` APIs.
- OpenViking: inject `client.find(...)` (`openviking_sdk.SyncHTTPClient`) or,
  once published, wrap Grok 1 `OpenVikingBackend.search` as `Recall`.
- Lifecycle should call `after_capture` after a successful local save. It
  must ignore contribution transport errors.
- Root CLI, `pyproject.toml`, and `.github/workflows` are not modified here.

## Not verified in this delivery

- Live xAI Grok quality
- Live GitHub API, Actions, or merge
- x86-64 Windows
- Native OpenViking instance in this worktree (adapter is present; tests use fixtures)
