# Federation

## Direction

Sync is one-directional per layer. There is no merge algorithm.

```
fork project layer  --PR-->  corpus/unverified/  --review-->  corpus/verified/
corpus/verified/    --periodic pull-->  every fork's shared cache (read-only)
```

A fork never writes to `verified/` directly, and this repo never writes into a
fork. Bidirectional reconciliation of a knowledge corpus is not worth its
complexity: the failure modes are silent overwrites of reviewed content.

## Granularity

Per entry, never per file. A `kind` document accumulates hundreds of entries
from unrelated contributors; file-level sync would turn every submission into a
YAML conflict and the conflicts would be resolved by whoever rebases last.

Sync keys:

| Field | Role |
|---|---|
| `uuid` | identity — stable across revisions and across forks |
| `content_hash` | revision — `sha256:` over the canonicalized `scope` + `rule` payload |
| `provenance.origin_repo` | which fork proposed this revision |

Two date rules that follow from the above and are easy to get wrong:

- A document's top-level `updated_at` is the **maximum** of its entries'
  `lifecycle.updated_at`. It is derived, never independently edited.
- The **proposer** advances `lifecycle.updated_at` when submitting a revision.
  The export gate rejects a revision whose `content_hash` changed while
  `lifecycle.updated_at` did not; it does not helpfully fill the date in.
  Auto-stamping would make the export depend on the exporting machine's clock,
  and two forks exporting the same entry would then produce two revisions.

## Idempotency

Re-exporting an unchanged entry must produce no PR. Re-exporting a changed
entry must produce an update to the same `uuid`, never a second entry.

Rules:

- same `uuid` + same `content_hash` → no-op
- same `uuid` + different `content_hash` → revision proposal, `lifecycle.updated_at` advances, `verification.last_verified_at` does **not**
- different `uuid` + near-identical `rule` → duplicate candidate, bot reports both; humans decide whether to merge with `lifecycle.supersedes`
- `uuid` unknown here → new entry, lands in `unverified/`

An unstable hash function turns every sync into a revision storm, so
canonicalization is specified exactly rather than left to an implementation:

0. **Validate first.** Hashing a schema-invalid entry is undefined. In
   particular, canonicalization must **never coerce types**: a YAML `min: 2.5`
   is a float where the schema requires a string, and an implementation that
   quietly stringifies it produces a different hash from one that does not.
   Reject it as invalid instead.
1. Take only the entry's `scope` and `rule` sub-objects. Nothing else — not
   `status`, not dates, not provenance. Re-verifying or re-reviewing an entry
   must not change its revision.
2. In `rule.fingerprints`, lowercase every item, strip leading/trailing
   whitespace, collapse internal whitespace runs to a single space, drop
   duplicates and empties, then sort.
3. In every other string **value**, recursively at any depth — including the
   strings nested inside `scope` constraints — strip leading/trailing whitespace
   and normalize line endings to `LF`. Do not otherwise reflow prose. Mapping
   **keys** are not normalized: they are fixed by the schema, which sets
   `additionalProperties: false` everywhere, so there is nothing to normalize
   and an implementation must not touch them.
4. Serialize `{"rule": …, "scope": …}` as JSON with `sort_keys=True`,
   `ensure_ascii=False`, and separators `(",", ":")`.
5. `content_hash` = `"sha256:" + sha256(utf8(that string)).hexdigest()`.

Step 1 is the one worth restating: the revision tracks what the entry *claims*,
not how it has been processed.

Steps 0 and 3 exist because two independent implementations read the earlier,
looser wording differently — one recursing into `scope`, one not. Both produced
plausible hashes. That is precisely the silent divergence that breaks per-entry
idempotency, so the wording is now exact rather than reasonable.

## Where the work happens

The fork runs schema validation and redaction **before** proposing. The main
repo's bot does only what needs the whole corpus:

- redaction re-scan under the current `redaction_profile`
- exact and near-duplicate detection across all entries
- coordinate conflict detection
- id / hash integrity
- staleness sweep

Splitting it this way keeps per-PR bot cost bounded as the corpus grows, and
means a contributor gets schema feedback locally in seconds instead of waiting
on CI.

## Re-scanning after a ruleset change

`provenance.redaction_profile` records which ruleset version cleared each entry
at export time. When the ruleset tightens to `r<N+1>`, everything below `r<N+1>`
is re-scanned in bulk rather than trusted. Entries that fail the new rules are
quarantined out of `verified/` until corrected.

## Deletion

Entries are not deleted through sync. A fact that stopped being true is
`resolved` (with `resolved_by`) or `deprecated`. Removal is a separate,
explicit main-repo operation — normally only for a redaction failure, which
also requires history remediation, not just a commit.
