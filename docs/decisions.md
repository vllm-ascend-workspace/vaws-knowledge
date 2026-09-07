# Contract decisions

Five implementations are being written against these contracts concurrently —
`tools/`, `bot/`, `server/`, `sync/`, `conformance/` — plus clients in
contributing forks. Two of them independently reported fourteen places where the
contracts were ambiguous enough that two reasonable readings diverge.

That is a useful signal rather than a nuisance: an ambiguity found by two
implementations at the same time is one that would otherwise have been resolved
differently in each, silently. This file records the resolutions so nobody has to
guess or ask.

Resolutions below are normative. Where a resolution lives in another document,
that document is authoritative and this file is the index.

---

## Resolved

### 1. Version ordering was completely unspecified

The coordinate's whole purpose is to let a reader mechanically decide whether an
entry applies, which reduces to comparing versions against `range` bounds — and
the schema only said "string or null". Three implementations each invented an
ordering; two of them disagreed about whether `8.0.RC10` is newer than
`8.0.RC2`.

**Resolution:** [docs/version-ordering.md](version-ordering.md). Per-dimension
schemes, natural-segment comparison for vendor versions, PEP 440 for the Python
packages, exact-match-only for the rest, and `undecidable` as a first-class
result that must never be collapsed into match or mismatch.

### 2. Canonicalization: recursion into `scope`

"In every other string" did not say whether it recursed into `scope` constraints.
One implementation recursed, one did not. Both produced plausible hashes.

**Resolution:** recursive, at any depth. It is now stated explicitly in
`docs/federation.md`. The recursive reading is also the one that reproduces the
`content_hash` recorded in `examples/valid-entry.yaml`, so it was already the de
facto contract.

### 3. Canonicalization: mapping keys

**Resolution:** values only. Keys are fixed by the schema, which sets
`additionalProperties: false` everywhere, so there is nothing to normalize and an
implementation must not touch them.

### 4. Canonicalization: non-string scalars

A YAML `min: 2.5` is a float where the schema requires a string. An
implementation that quietly stringifies it hashes differently from one that does
not.

**Resolution:** validate before hashing; never coerce. Hashing a schema-invalid
entry is undefined, and the validator must reject the float with a message that
names the float rather than emitting a generic type error.

### 5. Document `updated_at` versus entry `lifecycle.updated_at`

**Resolution:** the document's top-level `updated_at` is the maximum of its
entries' `lifecycle.updated_at`. It is derived, never independently edited.

### 6. Who advances `lifecycle.updated_at` on a revision

**Resolution:** the proposer. The export gate **rejects** a revision whose
`content_hash` changed while `lifecycle.updated_at` did not; it does not fill the
date in. Auto-stamping would make export depend on the exporting machine's clock,
and two forks exporting the same entry would produce two revisions.

### 7. What `layer` a fork's export carries

**Resolution:** always `unverified`. A fork cannot propose directly into the
verified corpus, so an export claiming otherwise is malformed rather than
ambitious.

### 8. "Submitter alone is not sufficient" — for which entries?

**Resolution:** enforced whenever `status` is `verified` or `stale`, **or** the
document's review zone is verified. `stale` is included deliberately: it asserts
the claim was once established, so it is held to the same evidence bar and is not
a parking spot for unevidenced claims. `tests/test_schema_contract.py` already
encodes that.

### 9. "A review bot is never a valid `verified_by` value" — which identities?

**Resolution:** any handle ending in `[bot]`, which is GitHub's own convention
for app identities, plus an explicit denylist maintained in exactly one tracked
place. The denylist belongs with the checks that enforce it (`bot/`), and other
implementations read it from there rather than keeping their own copy.

### 10. `evidence[].ref` had no per-type grammar

One implementation used heuristics. Three implementations using three different
heuristics would accept different things.

**Resolution:** the grammar below is normative now, and moves into the schema in
the batched change (#14).

| `type` | Accepted `ref` |
|---|---|
| `run_manifest` | a manifest id: no whitespace, `[A-Za-z0-9._-]+` |
| `pull_request` | `owner/repo#N`, `#N`, or an `https://` URL |
| `issue` | `owner/repo#N`, `#N`, or an `https://` URL |
| `commit` | 7–64 hex characters, `owner/repo@sha`, or an `https://` URL |
| `ci_run` | an `https://` URL, or `owner/repo:run-id` |

No form may contain whitespace. Prose is not a reference, which is the point of
the field.

### 11. Is `status: resolved` returned by default?

**Resolution:** yes, carrying its `resolved_by` reference, exactly as
`docs/lifecycle.md` states. That document is normative; where a task description
or a comment disagrees with it, the document wins. A resolved entry answers two
questions at once — what you are hitting, and what you need to move past it — so
withholding it by default would be actively unhelpful.

### 12. Staleness horizon had no number

**Resolution:** it is policy, not contract. It lives in the tracked
`bot/policy.yaml` and is reviewed like code; the current value is 180 days,
roughly two release cycles. The schema deliberately does not encode it, because
the right horizon changes with release cadence and a schema change is the wrong
instrument for that.

### 13. `verified_against` could omit dimensions that `scope` bounds

`scope` requires all twelve dimensions, while `verified_against` marked several
optional — so an entry could constrain `topology` in its coordinate without ever
recording the topology it was observed on. That is a hole in the audit trail.

**Resolution:** if a dimension is bounded in `scope` — that is, `values` or
`range` rather than `any` — the corresponding `verified_against` value is
**required**, with one exception below. Enforced as a validator rule rather than
in the schema, because expressing eleven cross-field conditionals in JSON Schema
would be unreadable for no gain.

**Exception: `component`.** It is not a property of an environment. It says which
subsystem the fact is *about* — service bootstrap, ssh transport, an attention
kernel — which is a classification of the claim, not something observed on a
host. Accordingly `concrete_environment` has no `component` field at all, and
requiring one would make the rule unsatisfiable.

This exception was found by checking the rule against
`examples/valid-entry.yaml`, which bounds `component` in its coordinate and
correctly omits it from `verified_against`. A rule that fails on the repository's
own reference fixture is a wrong rule, not a broken fixture.

---

## Deferred to one batched change

Both items below are breaking. They are deliberately **not** being applied while
five implementations are mid-flight: a field name that moves under an
implementation costs more than a few hours of documented confusion, and batching
them means one coordinated update instead of five moving targets.

### 14. `layer` carries two orthogonal meanings

The document-level `layer` is a **review zone** (`verified` | `unverified`, and
`tests/test_schema_contract.py` explicitly rejects `candidate` there). The
README's three layers are **trust layers** (`shared` | `project` | `candidate`).
The collision has a real consequence: a candidate-layer file cannot describe its
own trust layer, so trust has to be inferred from where it was mounted.

**Planned:** rename the document field to `review_zone`, and reserve "layer"
exclusively for the trust layer. Also fold in the schema-level `evidence[].ref`
patterns from #10, and forbid `range` on the exact-match-only dimensions named in
`docs/version-ordering.md`.

Until that lands, implementations keep using `layer` for the review zone and take
the trust layer from the mount, which is what they already do.

---

## Follow-ups

- The root `requirements.txt` needs `packaging` for the PEP 440 dimensions.
  Not added here to avoid conflicting with an open pull request that touches
  dependency files. Until it is present, an implementation must report
  `undecidable` for those dimensions rather than substituting another ordering —
  `docs/version-ordering.md` requires that, so the gap degrades safely.
- `conformance/` must carry vectors for every row of the version-ordering table,
  including the undecidable cases, so a new implementation cannot pass while
  disagreeing about ordering.
- The `README.md` layout section describes `tools/`, `bot/` and `server/` in the
  present tense. That was aspirational when written; as their pull requests land
  it becomes accurate, and it should be re-read against reality once they do.
