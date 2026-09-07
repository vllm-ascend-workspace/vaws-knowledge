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
2. In `rule.fingerprints`: lowercase **ASCII letters only** (`A`–`Z` → `a`–`z`,
   nothing else), strip leading and trailing ASCII whitespace, collapse internal
   runs of ASCII whitespace to one `U+0020`, drop duplicates and empties, then
   sort **byte-wise ascending over the UTF-8 encoding**.
3. In every other string **value**, recursively at any depth — including the
   strings nested inside `scope` constraints — normalize line endings to `LF`,
   strip trailing ASCII whitespace from every line, then strip leading and
   trailing ASCII whitespace from the whole value. Do not otherwise reflow
   prose. Mapping **keys** are not normalized: they are fixed by the schema,
   which sets `additionalProperties: false` everywhere, so there is nothing to
   normalize and an implementation must not touch them.

Three definitions that steps 2 and 3 depend on, spelled out because they are
where implementations in different languages silently disagree:

- **ASCII whitespace** means exactly `U+0009`, `U+000A`, `U+000B`, `U+000C`,
  `U+000D`, `U+0020`. Nothing else is whitespace for canonicalization purposes.
  A non-breaking space or an ideographic space inside a value is *content* and
  is preserved. Python implementations must therefore not use a bare
  `str.strip()` or `str.split()`, both of which also consume `U+00A0` and
  `U+3000`; an ASCII-only implementation in another language would not, and the
  two would hash differently.
- **Lowercasing is ASCII-only.** Full Unicode lowercasing needs a Unicode
  database, and implementations pinned to different Unicode versions produce
  different results. Worse, it is not even length-preserving: Python renders
  `İ` (`U+0130`) as `i` followed by a combining dot above, so a Python
  implementation and an ASCII-only one disagree on the byte length of the same
  fingerprint. Fingerprints are observable log signatures and are essentially
  always ASCII, so restricting the rule costs nothing real.
- **No Unicode normalization is applied.** Not NFC, not NFD, none. Requiring a
  normalization form would require every implementation to agree on a Unicode
  version; applying none cannot drift, because there is nothing to get wrong.
  The consequence is accepted deliberately: two visually identical strings in
  different normalization forms are different byte sequences and hash
  differently. That is the correct behaviour for a revision identifier, which is
  a statement about bytes; semantic duplicates are the job of the reviewed
  duplicate-detection step, not of the hash. The conformance kit found this the
  hard way, when one of its own vectors used a Cyrillic character that
  decomposed under NFD.

Byte-wise sorting in step 2 is specified in place of locale collation for the
same reason: a locale-dependent sort is divergent by definition. For well-formed
UTF-8 it coincides with code-point order, so either phrasing is implementable,
but byte-wise leaves nothing to interpret.
4. Serialize `{"rule": …, "scope": …}` as JSON with `sort_keys=True`,
   `ensure_ascii=False`, and separators `(",", ":")`.
5. `content_hash` = `"sha256:" + sha256(utf8(that string)).hexdigest()`.

Step 1 is the one worth restating: the revision tracks what the entry *claims*,
not how it has been processed.

Steps 0 and 3 exist because two independent implementations read the earlier,
looser wording differently — one recursing into `scope`, one not. Both produced
plausible hashes. That is precisely the silent divergence that breaks per-entry
idempotency, so the wording is now exact rather than reasonable.

## Field ownership

A revision proposal from a fork may only change the fields a fork owns. The rest
belong to the main repo and describe review state, which a proposer cannot know.

| Owner | Fields |
|---|---|
| fork | `scope`, `rule`, `slug`, `confidence`, `provenance` |
| main repo | `status`, `redaction_cleared_under`, `lifecycle.first_seen`, `lifecycle.supersedes`, `lifecycle.superseded_by`, `lifecycle.resolved_by`, `conflicts`, `verification` |
| derived | `content_hash`, and the document's `updated_at` |

`lifecycle.updated_at` is the one shared field: the proposer advances it (see
above), and the main repo does not rewrite it.

A proposal that changes a main-repo field is rejected rather than filtered. A
fork proposing `status: verified` is downgraded to `unverified` with its
`verification` record retained as evidence for whoever reviews it, and its
`confidence` capped at `medium`, because the schema forbids `high` on an
unverified claim.

## A revision targeting an already-verified entry

Two rules collide here: same `uuid` with a different `content_hash` is a
revision, and a fork never writes into `corpus/verified/`.

**Resolution: report it as a conflict, fail closed.** The proposal does not land,
and the formed revision is attached to the report so a maintainer can take it
through review deliberately. Silently applying it would let a fork edit reviewed
content, and silently dropping it would lose a correction to a fact that is
already being consumed — so it becomes a decision somebody makes, rather than one
the tooling makes for them.

## Duplicate candidates do not land by default

A near-identical `rule` under a different `uuid` is reported, not written.
Landing it automatically would grow the corpus with pairs nobody chose to keep
separate. Writing it is opt-in, and merging the pair is a human decision recorded
with `lifecycle.supersedes`.

## Snapshot determinism

"Reproducible" and "carries a generation timestamp" pull against each other. The
published snapshot resolves it by taking its timestamp from the corpus revision's
committer date, or `SOURCE_DATE_EPOCH` when set, so the same corpus always
produces byte-identical output. Stamping the actual wall clock is available but
explicit, and gives up reproducibility for that run.

## Corpus layout

`corpus/<review-zone>/<kind>.yaml`. Readers accept any `*.yaml` under a zone
directory, so a `kind` may be split across files when one grows unwieldy, and the
`layer` field inside each document must still agree with its directory.

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
quarantined out of `verified/` until corrected — and quarantine is a *proposal*
the re-scan emits, never an edit it performs.

An entry that **passes** the re-scan gets `redaction_cleared_under` set to the
new profile. This is a separate field from `provenance.redaction_profile` on
purpose: that one is an immutable historical fact about the export, so it cannot
be advanced, and without somewhere else to record the result a passing re-scan
would leave no trace. The consequences of conflating them are concrete — the same
entries would be re-scanned on every run forever, and a published snapshot's
profile floor could never rise above its oldest export.

The floor a snapshot advertises is therefore the minimum, across its entries, of
`redaction_cleared_under` where present and `provenance.redaction_profile`
otherwise.

## Deletion

Entries are not deleted through sync. A fact that stopped being true is
`resolved` (with `resolved_by`) or `deprecated`. Removal is a separate,
explicit main-repo operation — normally only for a redaction failure, which
also requires history remediation, not just a commit.
