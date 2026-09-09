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
| `content_hash` | revision — `sha256:` over the canonicalized body payload: `scope` + `rule` or `measurement` for runtime claims, `reference` alone for sourced material |
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
- different `uuid` + same `measurement` subject and coordinate → duplicate candidate on the same terms, compared by subject + quantity identity + coordinate rather than by prose
- different `uuid` + near-identical sourced `reference` → duplicate candidate on citation identity; a rule, a measurement and a reference are never duplicates of each other just because two empty rule strings compare equally
- …but if such a pair claims a **different value or unit** for a quantity identity it shares, it is a conflict rather than a duplicate: it is reported by the conflicts gate, blocks promotion, and is never offered as something to merge, because merging it would discard one of two irreconcilable numbers
- `uuid` unknown here → new entry, lands in `unverified/`

An unstable hash function turns every sync into a revision storm, so
canonicalization is specified exactly rather than left to an implementation:

0. **Validate first.** Hashing a schema-invalid entry is undefined. In
   particular, canonicalization must **never coerce types**: a YAML `min: 2.5`
   is a float where the schema requires a string, and an implementation that
   quietly stringifies it produces a different hash from one that does not.
   Reject it as invalid instead.
1. Take the entry's **body** sub-object, plus `scope` when the body is a
   runtime claim. The body is `rule`, `measurement`, or `reference`. The
   payload key is the body's own name, so a rule hashes byte-for-byte as it
   always did. A sourced reference hashes `{"reference": …}` only: it must
   not invent the twelve runtime coordinates. Nothing else — not `status`,
   not dates, not provenance. Re-verifying or re-reviewing an entry must not
   change its revision. An entry with neither body or with both is
   schema-invalid, so by step 0 its hash is undefined; an implementation
   must refuse it rather than pick one.
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
4. Serialize `{<body>: …, "scope": …}` as JSON with `sort_keys=True`,
   `ensure_ascii=False`, and separators `(",", ":")` — that is, `{"rule": …,
   "scope": …}` for a rule entry and `{"measurement": …, "scope": …}` for a
   measurement entry. A sourced `reference` hashes `{"reference": …}` only and
   must not invent the twelve runtime coordinates. The payload is keyed by the
   body's own name, which is why adding later body variants moved no existing
   rule or measurement `content_hash`.
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
| fork | `scope`, the body (`rule` or `measurement`), `slug`, `confidence`, `provenance` |
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

A near-identical body under a different `uuid` is reported, not written.
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

The splitting allowance is used by `kind: hardware-measurements`, which is two
files in `corpus/unverified/`: `hardware-measurements-theoretical-peaks.yaml`
(63 entries, one per SoC, quantities declared by the vendor's CANN
`platform_config`) and `hardware-measurements-sustained.yaml` (self-measured
fractions of those peaks). Same kind, separate files, because a vendor
declaration and a microbenchmark are reviewed on different grounds even though
they share the entry contract — and a re-snapshot of the vendor table should not
touch the measured file. Neither is in `corpus/verified/`: one contributor
submitted both and nobody else has re-derived any of it. See
[decisions.md](decisions.md) §24.

## Central collection (trusted default branch)

vaws-knowledge can also *collect* already-public knowledge YAML instead of
waiting for every fork to enable a workflow. `.github/workflows/collect.yml`
runs daily at 04:27 UTC (off-hour, finite; knowledge arrival is source-driven
rather than a months-long staleness horizon) and on `workflow_dispatch`.

Dispatch `preview` never writes. Scheduled runs, and dispatch `propose`, may
open candidate PRs **in this repository only** after the existing export,
schema, redaction, plan and propose gates pass. The collector never writes to
a contributing fork and does not require fork owners to enable Actions.

Discovery is bound to scaffold repository numeric id **1196723340**, resolved
to the current `full_name` on every run (`GET /repositories/1196723340`).
Names may change during an organization transfer. Each listed fork is
re-fetched and accepted only when its `source`/`parent` numeric id matches
that frozen parent; a name prefix is not identity. Private or unreachable
sources stay uninspected — they are not a reason to add credentials. The
source-side recipe is unchanged: run `tools/export.py` locally and open a PR.

Fetched bytes are stored outside the trusted checkout and are never placed on
`sys.path`. Only ordinary `100644` blobs under `.agents/knowledge/*.yaml` at a
**frozen** default-branch commit SHA are read. Executables, symlinks,
submodules, path traversal and oversize blobs are rejected before use.
v1 / incomplete / unknown scope is recorded as unsupported; the two migrated
scaffold entries with unresolved dimensions and the v1 model facts are
blocked, and zero eligible entries is an honest no-op.

Identical `uuid`+`content_hash` observations are deduplicated with every
source binding preserved. Conflicting revisions of the same identity are
reported, never last-writer-wins. `sync/plan.py` and `sync/propose.py
--open-pr` remain the upsert/ownership implementation. The collector will not
pass `--skip-gates`, `--drop-undeclared` or `--allow-duplicate-candidates`.

A public GitHub blob proves where bytes were read, not who the contributor is
and not whether the claim is true. Central re-scan does not prove that a
source-side scan occurred.

## Consuming a verified snapshot

`sync/snapshot.py` (and `.github/workflows/publish-snapshot.yml`) publish the
existing verified snapshot after schema/integrity **and** redaction gates.
`corpus/unverified/` is never published. An empty verified corpus is an
accurately labelled empty snapshot (`entry_count: 0`), not runtime evidence.

Import against an **exact** `corpus_revision` / `snapshot_digest`. The
accepted Phase B shared-cache rules still apply: only `corpus/verified/` (or
the snapshot's `verified/` directory) may enter shared; unverified and
project-zone documents cannot; source repo/ref must be recorded; a failed
refresh must preserve the last valid shared cache; query/get use the same
trust checks.

Import with the accepted Phase B client against an exact verified snapshot
directory or a checkout of `corpus/verified/` pinned to the snapshot's
`corpus_revision` (a 40-character commit). Do not point the shared cache at
`corpus/unverified/` or a project layer.

```
python3 .agents/scripts/knowledge_shared_cache.py [--shared-dir …] import \
  --from /path/to/verified-snapshot/verified \
  --source-repo vllm-ascend-workspace/vaws-knowledge \
  --source-ref <exact-40-character-commit>
```

Optional expected repo/ref checks and `status` / `query` verification use the
same importer-owned policy, source binding, and last-valid-cache preservation.
A central workflow can publish a snapshot and inspect forks; it cannot
populate another developer's untracked `.vaws-local/knowledge/shared/` by
itself. An optional copy-yourself template lives at
`docs/periodic-pull-template.yml`. That template only prints import
instructions; it does not refresh a cache. Do not install it into other
repositories from here. GitHub schedules in new public forks are disabled by
default; central collection does not depend on those schedules being enabled.

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
