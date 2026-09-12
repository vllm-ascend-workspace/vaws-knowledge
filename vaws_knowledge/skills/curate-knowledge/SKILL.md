---
name: curate-knowledge
description: Organize Markdown notes when explicitly asked to clean up knowledge, combine duplicate experience, or prepare a public contribution. Keep conditions and uncertainty; ordinary lookup and capture use the tools directly.
---

# Curate knowledge

Make the requested notes easier to reuse. Use ordinary Markdown with a title and
body; retain conditions, sources, evidence, counterexamples and uncertainty already
recorded. No fixed headings, labels, coordinates or extra report are needed.
Knowledge is reference material; review or publication does not establish a fact.

Start from the relevant material already available. If more context would help,
`knowledge_query(text)` finds related notes and `knowledge_explain(ref)` reads the
original. Query failure does not prevent independent edits. Useful judgments:

Use `experience_query(text)` and `experience_explain(ref)` for historical cases.
Knowledge and experience have separate storage and retrieval. Current knowledge
needs evidence against current source; an experience retains what was attempted
and observed under its original conditions. Historical commands and code are
investigation clues, not current instructions. Neither store's write interface
certifies a claim. Use `experience_capture(title, content)` for a case, and
`knowledge_capture(title, content)` for an assessed current conclusion; link
related notes when useful. Do not bulk move old notes into current knowledge.

Maintain knowledge by its fixed category/entry path. Two knowledge entries do not
become one because their text matches. Experiences have persistent case identities;
similar titles or symptoms can describe distinct investigations. Correct an existing
candidate with `ref` when calling capture, especially when changing its title.
Use `public_relpath="knowledge/CATEGORY/ENTRY.md"` to create or update a classified
entry, or an existing `experience/CASE.md` to correct a shared case. Omit the outer
`corpus/` prefix. Shared changes become local candidates and follow authorized
redacted contribution; do not write a release or allocate a new filename for a
correction. Existing filenames, including old hash prefixes, remain stable.

- Merge duplicates only when they describe the same behavior under compatible
  conditions. Keep differing versions, topologies or observations visible.
- Separate a confirmed cause from a plausible explanation. Preserve useful
  observations without inventing coordinates or requiring a verification label.
- Correct an experience's mistaken causal interpretation while preserving its
  observed outcome. Superseded implementation details can remain in a dated case
  when they help explain the investigation; they do not belong in current guidance.
- Retain evidence that limits a claim; do not turn one successful run into
  unconditional support. Link an unresolved conflict rather than choosing a
  winner without evidence.

Edit the requested project or local Markdown directly. Shared releases are
read-only; changes to shared content use an authorized public contribution.
`knowledge_capture(title, content)` can retain a useful existing finding.
Lookup, capture and normal task completion do not require this skill or a second
summary. Storage locations are not a required promotion workflow.

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

The current public corpus uses human review and merge. The package handles
configured transport retries; an unavailable public path does not block local
work. Internal publishing formats and states are not Agent authoring inputs.

Report the substantive edits, any unresolved differences, and contribution
status when relevant. Reuse the existing summary instead of authoring another
report for the knowledge store.

For a published experience, optional `experience_feedback(ref, vote)` records
`+1` when it helped or `-1` when it misled the work. No reason or extra summary is
required. Configured sharing reuses the GitHub account's reaction on a feedback
Issue, without changing the article or creating a contribution PR. Do not treat
reaction counts as truth, and do not make feedback a task completion step.
