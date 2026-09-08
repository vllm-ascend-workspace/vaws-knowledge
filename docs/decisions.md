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

### 22. A hardware measurement is an entry with no symptom

`README.md` says an entry is "a claim about a specific coordinate in a specific
environment, carrying a followable reference to the run that established it". A
measured FP16 peak of 2.70 TFLOPS for `Ascend031` is exactly that: a value, a
coordinate, and a run that produced it. The schema disagreed. The body was
`rule`, whose `summary`, `symptom`, `root_cause` and `resolution` are all
required — mirrored in `RULE_BODY_FIELDS` in `bot/corpus.py` and in
`PAYLOAD_KEYS` in `conformance/reference.py`. A measurement has no symptom and
nothing to resolve.

So the two rules that collide are the README's definition of an entry and the
schema's definition of a body. The consequence was not abstract: hardware facts
stayed outside the commons, duplicated inside a single consumer as
`hardware_theoretical_peaks_cann9_0_0.json` (63 rows) and
`hardware_peak_measurements.json` — the duplication the repository split existed
to end.

**Resolution:** a second payload variant inside the same entry envelope. Every
field that makes an entry trustworthy is unchanged and still required — `uuid`,
`content_hash`, `scope` over all twelve dimensions, `provenance`, `lifecycle`,
`confidence`, `status`, `slug`, `layer`. Only the body differs, and the schema
enforces exactly one of `rule` and `measurement` per entry via `oneOf`, so
"neither" and "both" are unrepresentable rather than merely discouraged. The
`measurement` body carries the subject (SoC id, aliases, family), the method
that established the claim (`vendor_platform_config` or `microbenchmark`, with
its parameters and a source reference), and a list of quantities, each with a
`name`, a `basis`, a `value`, a `unit` and an optional `qualifier`.

**A second repository was rejected.** It reproduces the duplication one level
up. The coordinate, the redaction profile, the canonicalization, the review
zones, the promotion rules, the conflict semantics and the query surface would
all have to be re-implemented and then kept in step by hand; the first
divergence between the two copies would be silent. Nothing about a measurement
needs a different envelope — it needs a different body.

**Forcing measurements into rule shape was rejected.** It is available and
cheap: put "FP16 peak is 2.70 TFLOPS" in `summary`, leave `symptom` as "n/a".
The cost is that `symptom` stops meaning anything. Every gate that reads the
rule body — dedup's prose similarity above all — would then be comparing filler,
and the fields whose whole purpose is to be reviewed as claims would be carrying
placeholders. A schema that permits "n/a" in a required field has a required
field in name only.

### 23. Entry granularity for measurements: per subject, not per quantity

63 theoretical rows carry roughly five quantities each. The rule that entry
granularity is a free choice collides with the rule that dedup and conflict
granularity *follow* entry granularity: whatever an entry is, that is the unit a
conflict can be reported about, the unit that gets one `content_hash`, one
`provenance` and one `lifecycle`, and the unit a reviewer promotes or retires.

**Resolution:** one entry per subject per method — 63 theoretical entries, one
per SoC, each bundling that SoC's quantities, plus one sustained entry for the
one measured subject. This matches the shape of the fact: all of one SoC's peaks
came from one `platform_config` snapshot at one CANN version, so they share one
provenance and one evidence reference, and a correction to that snapshot is one
revision rather than five.

The consequence for conflicts is that a contradiction is detected *inside* a
pair of entries, not between them: `bot/conflicts.py` compares two measurement
entries about the same subject at overlapping coordinates and reports every
quantity whose `(name, basis)` identity matches while its value or unit differs.
Bundling therefore does not weaken conflict detection, it only means one blocking
finding can name several quantities. It does mean two contributors who each
measure a different subset of one SoC's quantities produce two entries that must
be merged by review rather than automatically, which is the honest outcome:
they are two different runs.

**One entry per quantity was rejected.** It yields well over 300 entries whose
`scope`, `provenance` and evidence reference are identical, differing only in a
name and a number. Dedup would have to be taught that near-identical entries are
expected rather than suspicious, which is precisely the signal dedup exists to
raise. And a re-snapshot of one SoC becomes five revisions that can land
partially, leaving a coordinate where two of five peaks came from one CANN
version and three from another.

**One entry per source file was also rejected.** A single entry carrying all 63
SoCs would give the whole vendor table one `content_hash`, so correcting one
SoC's peak invalidates the hash covering the other 62, and no conflict could
ever be attributed to a subject.

### 24. Which zone vendor-declared and self-measured numbers land in

`CONTRIBUTING.md` requires that `verified/` carry a reference someone can follow
and a confirmation from someone other than the submitter. Against that stands
the intuition that these are not opinions: the theoretical peaks are the
vendor's own `platform_config` values, and the sustained numbers came off real
silicon with a stated method. Both rules are about trust, and they point in
opposite directions.

**Resolution:** `corpus/unverified/`, `status: unverified`, `confidence: low`,
and no `verified_against` block. Three reasons, in decreasing order of force.
The confirmation does not exist — one contributor submitted all 64 entries and
nobody else has re-derived any of them. The reference is not followable by a
third party: the run identifiers had to be scrubbed of an internal machine
identity to be publishable at all (see #25), so what remains is a method
description, which is enough to *re-measure* and not enough to *audit*. And a
vendor declaration is a claim about a part number, not an observation of a
machine: `platform_config` is a configuration file, it can be wrong, and reading
it correctly is not the same as confirming it.

Being off real hardware is not the criterion for `verified/` and must not become
one. The criterion is that someone other than the submitter followed the
evidence and agreed. Promotion is available the moment that happens, and the
`notes` on the sustained entry state exactly which unrecorded stack dimensions
would have to be pinned first.

### 25. Keeping `content_hash` stable while the payload gained a variant

`conformance/reference.py` fixed the hashed payload as `PAYLOAD_KEYS = ("rule",
"scope")`. Two rules collide: the new body must participate in the hash — a
`content_hash` that cannot see the number is worthless, since 2.70 could become
27.0 without moving it — and no existing entry's hash may move, because those
hashes are cited in `examples/valid-entry.yaml`, in the conformance anchor, and
in every fork's snapshot.

**Resolution:** the payload is `{scope, <body>}`, where the body key is
whichever of `rule` and `measurement` the entry actually has. For a rule entry
that produces byte-for-byte the same payload as before, so the hash is
unchanged by construction rather than by luck. Canonicalization itself did not
change at all: the same four steps apply to the new body's strings, at any
depth, and `sync/_common.py`, `tools/canonical.py` and `server/capture.py` all
resolve the body key the same way.

The proof is mechanical and was run both ways: every hash in `examples/`,
`conformance/vectors/` and `tests/fixtures/` recomputes to its recorded value
after the change, and `tests/test_conformance_vectors.py` now asserts that the
measurement vector's payload is keyed by `measurement` with no `rule` key
anywhere in it, so a future refactor that introduces a wrapper key fails a test
instead of silently rewriting every hash in the commons.

One consequence had to be accepted deliberately. JSON number formatting is not
portable — a float that Python renders `2.70336` may be rendered `2.7033600000`
elsewhere — and a hash that depends on it would be unreproducible across
languages, which is the one thing the conformance kit exists to prevent. So
every measurement `value` is a **string** carrying a numeric pattern, exactly as
version bounds already are, and `tools/validate.py` rejects a YAML float there
by naming the float. Consumers parse; the commons stores digits.

**A wrapper key was rejected.** Nesting both variants under a single `body` key
is tidier and rewrites the `content_hash` of every entry that exists, including
the conformance anchor — a migration whose only benefit is aesthetic.

**Leaving the measurement out of the hashed payload was rejected** for the
reason above: it would make the integrity gate blind to the only part of a
measurement entry anybody cares about.

### 26. Is a new body variant a breaking change for consumers?

Additively adding an optional field is compatible; removing a required one is
not. A new body variant is neither, and reading it as "additive, therefore
ignorable" is wrong. A v1 consumer does not encounter an entry with an extra
field it can skip. It encounters an entry with **no `rule` key at all**, and the
straightforward v1 client — `entry["rule"]["summary"]` — raises. Even the
careful one that uses `.get("rule", {})` silently treats a measurement entry as
an empty rule and may then report it as a duplicate of every other one.

**Resolution:** `service_api_version` is 2, with `supports: [1, 2]`.
`service-api.json` now declares `bodies: ["rule", "measurement"]` and, in
`v1_result_population`, the exact argument that reproduces v1 behaviour:
`knowledge_query` gained a `bodies` filter, so a v1 consumer passes
`bodies: ["rule"]` and sees precisely the corpus it saw before. Query results
carry a `body` discriminator, and the rule-only fields are `null` rather than
absent on a measurement result, so a consumer that does look can tell the
difference between "not a rule" and "a rule with a missing field".

**Arguing compatibility was rejected** because it would put the cost on the
side that cannot pay it. A consumer cannot detect this change: nothing in a v1
response tells it that the corpus now contains a shape it does not model. The
version number is the only channel that reaches it before it reads bad data.
Bumping when the answer is arguable costs one number; not bumping costs a silent
misread in somebody else's analyzer.

### 27. "Unresolved dimension" turned out to have no representation

The brief for the measurement migration said that dimensions which cannot be
established must be "recorded as unresolved with a `needs` string, exactly as
existing entries do". Two rules collide here, and one of them does not exist:
`scope` admits exactly three constraint forms — `any` + `basis`, `values`, and
`range` — with `additionalProperties: false` on each, so a `needs` key is
schema-invalid. There is also no existing corpus entry to copy: before this
change `corpus/` contained only `.gitkeep` files. `needs` is a field in the
*scaffold's* local `.agents/knowledge/` v2 files, not in this contract.

**Resolution:** an unestablished dimension is `range: {min: null, max: null}`,
and the justification goes in the body, which is what the schema's own
description of `range` already asks for ("a claim about untested territory
[that] should be justified in the rule body"). The sustained entry's `notes` name
the four dimensions that were not recorded and say what re-measuring would fix.
Nothing was invented to fill a dimension.

This exposed a real defect, which is fixed here rather than left for the
migration to work around: `server/query.py` resolved a both-sides-null range to
`covered`, so an unestablished dimension matched *every* query and untested
territory was reported as confirmed applicability. It now resolves to
`undecidable`, which is the pre-existing first-class result from #1 for exactly
this situation.

`bot/conflicts.py` reads the same shape the opposite way — it treats a
both-sides-null range as overlapping, and says so in the relation's `reason`.
That is deliberate, not an inconsistency left in by accident: each module is
conservative in its own direction. A query must not report untested territory
as applicable, and a conflict gate must not let a pair escape review by
declining to bound a dimension. Both readings refuse to give the entry credit
for the dimension it never established.

**Adding a fourth constraint form was rejected**, for now. It is the better
long-term answer — "not established, and here is what would establish it" is a
distinct state from "unbounded on both sides" and deserves to be
machine-readable — but it changes the `constraint` definition that every
implementation and every stored entry depends on. It belongs in the batched
breaking change below, next to `layer` → `review_zone`, not in a migration.

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
