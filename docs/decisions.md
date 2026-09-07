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

### 14. A passing re-scan had nowhere to record its result

Reported while implementing `sync/rescan.py`, and it is a genuine hole in the
design rather than an ambiguity in its wording.

`docs/federation.md` specified that when the redaction ruleset tightens, every
entry recorded under an older profile is re-scanned rather than trusted. But
`provenance.redaction_profile` is defined as the profile applied *at export
time* — an immutable historical fact about the export, which cannot be advanced.
So an entry that passed the new scan had no field in which to say so.

The consequences are concrete: the same entries would be re-scanned on every run
forever, and a published snapshot's advertised profile floor could never rise
above its oldest export, which makes the floor useless as a signal.

**Resolution:** a new optional, main-repo-owned `redaction_cleared_under` on the
entry, recording the highest ruleset the entry has been re-scanned clean under.
It is deliberately **not** part of the `content_hash` payload — it describes how
the entry was processed, not what it claims, which is the same rule that keeps
review and re-verification from changing a revision. A snapshot's floor is the
minimum across its entries of `redaction_cleared_under` where present and
`provenance.redaction_profile` otherwise.

This is additive and optional, so it does not break the implementations already
written: they validate against the shipped schema file rather than a hardcoded
field list, so the field becomes valid everywhere at once.

### 15. A revision targeting an entry that is already verified

Two rules collide: same `uuid` with a different `content_hash` is a revision, and
a fork never writes into `corpus/verified/`.

**Resolution:** report it as a conflict and fail closed, with the formed revision
attached to the report so a maintainer can take it through review deliberately.
Applying it silently would let a fork edit reviewed content; dropping it silently
would lose a correction to a fact that is already being consumed. It becomes a
decision somebody makes rather than one the tooling makes for them.

### 16. What a fork's export may own

**Resolution:** the ownership table now in `docs/federation.md`. Forks own
`scope`, `rule`, `slug`, `confidence` and `provenance`; the main repo owns
`status`, `redaction_cleared_under`, the review-state parts of `lifecycle`,
`conflicts` and `verification`. A proposal touching a main-repo field is rejected
rather than quietly filtered, because filtering would make the proposer believe
something landed that did not.

A fork proposing `status: verified` is downgraded to `unverified` with its
`verification` record kept as evidence for the reviewer, and `confidence` capped
at `medium` — the schema forbids `high` on an unverified claim, so something has
to give, and capping confidence is less lossy than discarding the evidence.

### 17. Duplicate candidates do not land by default

**Resolution:** reported, not written; landing one is opt-in, and merging a pair
is a human decision recorded with `lifecycle.supersedes`. Landing them
automatically would grow the corpus with near-duplicate pairs nobody chose to
keep separate.

### 18. Reproducible snapshots versus a generation timestamp

These pull against each other by nature.

**Resolution:** the snapshot timestamp comes from the corpus revision's committer
date, or `SOURCE_DATE_EPOCH` when set, so the same corpus always yields
byte-identical output. Stamping the wall clock stays available but must be asked
for explicitly, and gives up reproducibility for that run.

### 19. Corpus file layout was unspecified

**Resolution:** `corpus/<review-zone>/<kind>.yaml`, with readers accepting any
`*.yaml` under a zone directory so a large `kind` can be split across files. The
`layer` field inside each document must still agree with its directory.

### 20. Canonicalization left five micro-rules unpinned

Reported by the conformance kit, which deliberately wrote no vectors for them
rather than letting the suite become the specification. All five are places where
implementations in different languages diverge on characters nobody looks at.

**Resolutions, all now in `docs/federation.md`:**

- **Whitespace is exactly the six ASCII characters.** A non-breaking space or an
  ideographic space inside a value is content and is preserved. This one is a
  live trap rather than a hypothetical: Python's bare `str.strip()` and
  `str.split()` also consume `U+00A0` and `U+3000`, so a Python implementation
  and an ASCII-only implementation in another language hash the same input
  differently.
- **Lowercasing is ASCII-only.** Full Unicode lowercasing needs a Unicode
  database, so implementations on different Unicode versions differ — and it is
  not length-preserving: Python renders `İ` as `i` plus a combining dot above,
  so two implementations disagree on the byte length of the same fingerprint.
- **No Unicode normalization at all.** Requiring a form would require agreement
  on a Unicode version; requiring none cannot drift. Two visually identical
  strings in different forms therefore hash differently, which is correct for a
  revision identifier — it is a statement about bytes. Semantic duplicates are
  the reviewed duplicate-detection step's job.
- **Fingerprints sort byte-wise over UTF-8**, not by locale collation, which is
  divergent by definition.
- **Trailing ASCII whitespace is stripped from every line** of a multi-line
  value, in addition to the whole-value strip. Trailing whitespace is invisible
  and editors add it, so it must not change a revision. Leading whitespace on a
  line is meaningful — indentation inside a resolution snippet — and is kept.

Pinning these leaves the anchor hash in `examples/valid-entry.yaml` unchanged,
which was checked rather than assumed: the fixture's payload contains no
non-ASCII character at all, so there was nothing for the loose and strict
readings to disagree about. Had the anchor moved, every implementation would have
had to re-derive it.

### 21. Do reserved documentation IP ranges count as "IP addresses"?

`CONTRIBUTING.md` banned IP addresses unconditionally. The screening tool
deliberately exempts the RFC 5737 documentation ranges, on the sound reasoning
that a non-routable address identifies nobody — and fixtures that prove the
screening works need a rejectable value to feed it.

**Resolution:** the exemption is right, so the prose was wrong. `CONTRIBUTING.md`
now states it explicitly, limited to the documentation ranges,
`2001:db8::/32`, `example.invalid` / `example.com`, and loopback. Other reserved
ranges are **not** exempt — `198.18.0.0/15` benchmarking space does turn up in
real internal networks.

The general point matters more than the specific ranges: a rule that disagrees
with its own enforcement is worse than either alternative, because readers follow
the prose and tools follow the code, and the gap between them is where a real
address eventually slips through. This should not have been settled by whichever
gate shipped first.

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
