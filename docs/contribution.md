# Public knowledge contribution

Status: current

Public contribution is optional and follows the user's existing authorization
and configuration. Ordinary lookup, capture and development need no fork,
review workflow or publishing follow-up. The public corpus uses human review
and merge. Its notes remain references; publication does not prove a claim or
decide whether it applies to the reader's environment.

## Authoring

Use a title and a non-empty Markdown body. Retain known conditions, source,
evidence and uncertainty in the prose. No fixed headings, frontmatter, UUID,
type selection, runtime coordinate or verification label is required.

```markdown
# Graph replay observation

Eager passed for the tested input; graph replay differed after its layout changed.
This was observed in one environment. Other versions were not checked.
```

Scope conclusions to the available evidence. Compatible duplicates can be
combined during an explicit editing task; retain differing observations and
unresolved explanations instead of turning one run into a universal rule.

## Local copy and submission

The contribution package preserves the local source and prepares a redacted
public copy. Only that copy may leave local storage. A redaction failure stops
the export, while local notes and independent development remain usable.

Configured publishing saves captures locally, prepares their public copy,
and retries submission through a dedicated contribution clone. Each public path
identifies one document. Knowledge uses a fixed semantic entry path, optionally
under a category such as `knowledge/graph/buffers.md`. It is updated by path,
not merged with another entry because their text happens to match. An experience
receives a case ID and initial title slug once. A matching case ID prevents
duplicate submissions; a similar title or symptom does not establish the same case.

Title, body and redaction changes preserve the assigned path, including existing
filenames with old hash prefixes. The current content digest detects changed
bytes and checks integrity; it is not document identity. An unchanged revision
reuses its submission. A correction updates the open PR; after a PR closes or
merges, new content starts a new PR from current upstream at the same path.
An unchanged capture does not reopen a closed PR. Authentication or
network failure preserves pending work; it does not fail the local capture.
Existing private candidates are not uploaded merely because sharing is enabled.

For a local correction, pass the existing candidate `ref` to either capture tool;
this also preserves identity when changing the title. For a classified knowledge
entry, pass `public_relpath="knowledge/graph/buffers.md"` to create or update it.
To correct a shared case from another Agent, pass its existing
`public_relpath="experience/CASE.md"`. Both write a local candidate and use the
normal redacted contribution path; shared releases remain read-only. A missing
experience target blocks submission rather than silently adding a new case.
Direct preparation accepts the same `--public-relpath` option. Paths omit the
repository's outer `corpus/` prefix. Title and body remain the only required inputs.

Use [publishing setup](publishing.md) for an explicitly requested installation or
maintenance operation. `python -m vaws_knowledge contribution prepare --help`
describes direct preparation; package code handles identity, integrity and
submission records. Agents do not author those records or call each internal
step during ordinary work.

## Review and releases

Human reviewers assess the Markdown diff and preserve meaningful conditions,
evidence and uncertainty. Review can improve the text without establishing
hardware truth. A merged corpus commit can be built and distributed as a shared
Release through [the distribution module](distribution.md).

This path adds no author requirements, trust tiers, conflict classifications or
mandatory Agent decisions beyond the Markdown content and authorized public copy.
