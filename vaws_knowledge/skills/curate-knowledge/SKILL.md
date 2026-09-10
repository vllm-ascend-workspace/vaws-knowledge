---
name: curate-knowledge
description: Review and consolidate Markdown knowledge when asked to organize experience, resolve conflicting notes, or prepare a public contribution. Ordinary lookup and capture use the knowledge tools directly.
---

# Curate knowledge

Make the requested knowledge easier to reuse while preserving the conditions,
sources, counterexamples and uncertainty actually recorded. A public review
decision does not establish a hardware fact.

Query related material with `knowledge_query` and read relevant documents with
`knowledge_explain`. Compare the claims before editing:

- Merge duplicates only when they describe the same behavior under compatible
  conditions. Keep differing versions, topologies or observations visible.
- Separate a confirmed cause from a plausible explanation. Preserve useful
  observations without inventing coordinates or requiring a verification label.
- Retain evidence that limits a claim; do not turn one successful run into
  unconditional support. Link an unresolved conflict rather than choosing a
  winner without evidence.

Edit the requested project or candidate Markdown. Shared releases are read-only;
changes to shared content go through its contribution workflow. Normal lookup,
capture and development do not require this skill or a second summary.

## Public contributions

When contribution is authorized, prepare a public copy through
`python -m vaws_knowledge contribution prepare`. Read that command's `--help`
for current arguments. The package owns redaction, Git identity, pending state
and submission; publish only its prepared public copy. Keep private addresses,
paths, credentials and machine identifiers out of public content.

Configured publishing can submit new captures and synchronize shared releases
in the background. Do not submit the same candidate again or wait for its PR
from an unrelated development task. Existing private candidates are not an
implicit authorization to upload them.

The current public corpus uses human review and merge. Optional automated review
is used only when actually configured; do not prescribe its CLI as a mandatory
step. A transport failure leaves the pending contribution for retry.

Report the substantive edits, any unresolved differences, and contribution
status when relevant. Reuse the existing summary instead of authoring another
report for the knowledge store.
