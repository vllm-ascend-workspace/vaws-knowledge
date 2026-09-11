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

Configured publishing saves new captures locally, prepares their public copy,
and retries submission through a dedicated contribution clone. Repeated delivery
of the same public content reuses the submission and PR. Authentication or
network failure preserves pending work; it does not fail the local capture.
Existing private candidates are not uploaded merely because sharing is enabled.

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
