# Conformance kit

Federation only works if independently written implementations agree byte for
byte. This repository already contains at least three places that compute
`content_hash` — `tools/canonical.py`, a fallback in `server/capture.py`, a
fallback in `sync/` — and every contributing fork computes it a fourth time
before proposing an entry. If any two disagree by one byte, the same entry
arrives under two revisions and per-entry idempotency, which the whole sync
design rests on, stops working without anybody noticing.

This kit is how an implementation proves it agrees. It is data plus a runner,
it knows nothing about the implementation it tests, and it can be vendored into
a fork that has none of this repository's code.

## Contents

```
vectors/        canonicalization test vectors: input entry, expected canonical
                payload, expected content_hash
gate_vectors/   gate test vectors: input document, expected verdict
                (deliberately synthetic bad values — see that directory's README)
runner.py       runs vectors against an implementation given as a command line
selfcheck.py    checks the kit against itself, wired into tests/
reference.py    a readable rendering of the canonicalization, used to produce
                the expected values
```

Requirements: Python 3.11+ and PyYAML. Nothing else — no `jsonschema`, no
`torch`, no hardware. `selfcheck.py` will additionally validate the gate
vectors against `schemas/knowledge-v2.schema.json` if `jsonschema` happens to
be installed, and says so plainly when it is not.

The kit imports nothing from `tools/`, `bot/`, `server/` or `sync/`, and
`selfcheck.py` fails if a kit file ever starts to. Those are the things under
test; they may not exist in the checkout where the kit runs.

## Running it against your own client

Your implementation is supplied as a **command**. Four commands are
recognised; give the ones you have.

| Flag | Reads on stdin | Must print on stdout |
|---|---|---|
| `--hash-cmd` | one entry | `sha256:<64 hex>` |
| `--payload-cmd` | one entry | the canonical JSON payload (optional, for diffs) |
| `--redaction-cmd` | one document | one verdict token: `accept` or `reject` |
| `--schema-cmd` | one document | one verdict token: `accept` or `reject` |
| `--export-cmd` | one document | the exported bytes |

Details:

- An entry arrives as a YAML mapping by default; pass
  `--input-format entry-json` if JSON is easier. Documents arrive as
  `document-yaml` by default, `--gate-format document-json` otherwise.
- Hash/export stderr is ignored, so those commands may log freely. Gate
  stderr is not a verdict; a protocol failure report may include one
  bounded line of it. Do not write the input document or secrets there.
- Gate stdout is a machine contract: exactly one verdict token. Surrounding
  ASCII whitespace and an optional trailing newline are ignored. Extra
  prose, extra lines, or a non-token is a protocol failure, not a parse
  hint. Documented spellings are `accept` and `reject`. Unambiguous aliases
  already recognised by the runner remain: `accepted` / `ok` / `pass` /
  `passed` / `valid` / `clean` for accept, and `rejected` / `refuse` /
  `refused` / `fail` / `failed` / `invalid` for reject.
- **Compatibility break.** Exit status is never itself a verdict. An
  adapter that only exits 0/1, or that exits non-zero because it could not
  start, used to make every negative vector PASS. That guessing is gone and
  there is no flag that restores it. Gate acceptance requires a completed
  process, exit 0, and an explicit accept token. Gate rejection requires a
  completed process, exit 0 or 1, and an explicit reject token. Exit 0
  with `reject` stays legal (simple adapters). Exit 1 with `reject` stays
  legal (the bundled schema fixture). No token, malformed stdout,
  contradictory token/exit pairs, exit 2 or higher, command-not-found
  126/127, signal termination, timeout, and spawn failure are
  execution/protocol failures. They FAIL the row even when the vector
  expected `reject`. A token printed before a timeout is not a completed
  result.
- `--export-cmd` is run **twice per vector** and the two stdout byte strings
  are compared. The kit stores no expected export output: the corpus file
  format is your business, its stability is the federation's business.
- Exit status of the runner itself is unchanged: `0` everything run
  passed, `1` a vector failed, `2` a usage or vector-loading problem.
  Vector classes with no command stay SKIP; an unconfigured gate is not
  PASS.

Examples:

```bash
# this repository: drive the real tools/server/sync APIs in a subprocess.
# Do not point --hash-cmd at conformance/reference.py and call that a pass.
python3 conformance/runner.py \
  --hash-cmd "python3 tests/fixtures/conformance/impl_tools.py" \
  --payload-cmd "python3 tests/fixtures/conformance/impl_tools.py --payload"
python3 conformance/runner.py \
  --hash-cmd "python3 tests/fixtures/conformance/impl_server.py"
python3 conformance/runner.py \
  --hash-cmd "python3 tests/fixtures/conformance/impl_sync.py"

# schema / redaction: tools/validate.py and tools/redact.py take a file
# path and return a structured result; they are not gate commands. The
# adapter below maps a *completed* ValidationResult / findings list onto
# one token and prints no token if the tool never returns that result:
python3 conformance/runner.py \
  --schema-cmd "python3 tests/fixtures/conformance/gate_tools_adapter.py schema" \
  --redaction-cmd "python3 tests/fixtures/conformance/gate_tools_adapter.py redaction"

# a fork's own client, in any language — the client must print accept or
# reject; do not wrap an exit-only validator without an adapter
python3 conformance/runner.py \
  --hash-cmd "./my-fork-client hash --stdin" \
  --schema-cmd "./my-fork-client validate --stdin" \
  --export-cmd "./my-fork-client export --stdin"

# what is in the kit, and one vector at a time while debugging
python3 conformance/runner.py --list
python3 conformance/runner.py --hash-cmd "..." --only fingerprints
```

The adapter recipe (`tests/fixtures/conformance/gate_tools_adapter.py`) is
the one this repository uses against the real tools:

1. Read the document from stdin and write it to a temporary `.yaml` file
   — that is the input format `tools/validate.py` and `tools/redact.py`
   actually support. They do not read stdin, and they do not accept `-`
   as a file.
2. Call the Python API on that path: `tools.validate.validate_paths` or
   `tools.redact.scan_file`. Do not wrap the CLI and guess from its exit
   status. A process that exits 1 because `jsonschema` raised during
   import has not validated anything.
3. Print `accept` only when that call returns a completed
   `ValidationResult` with `ok` true, or a findings list that is empty.
   Print `reject` only when it returns `ValidationResult.ok` false, or a
   non-empty findings list. Forward finding text on stderr, never on
   stdout.
4. If import, `RuntimeError`, `OSError`, `ToolError`, or any other
   exception occurs before a structured result is returned, print no
   token and exit non-zero. The runner records that as a protocol
   failure. A function that never returns cannot authorize `reject`.

A copy-paste adapter that shells out to `tools/validate.py --stdin`,
passes `-` as a filename, or maps any CLI exit 1 onto `reject` is the
failure mode this contract exists to close. Do not add a "legacy"
guess-from-exit flag.

A failure prints the expected and actual hash, and — if `--payload-cmd` is
available — the character offset where your canonical payload first diverges
from the expected one, which is almost always enough to identify which of the
five canonicalization steps went wrong.

Check the kit itself before trusting a verdict:

```bash
python3 conformance/selfcheck.py
python3 -m unittest discover -s tests
```

## What a passing run establishes

**It establishes format agreement, and nothing else.** Specifically:

- your implementation computes the same `content_hash` as everyone else for the
  entries in `vectors/`, so a re-export of an unchanged entry will be a no-op
  in sync rather than a spurious revision;
- your gates refuse the five redaction shapes and the four schema cases in
  `gate_vectors/`, and still accept a clean document;
- your exporter is byte-stable across two runs on the same input.

**It does not establish any of the following.**

- **That any knowledge entry is true.** Nothing in this kit reads a claim.
  Every version, SoC, driver and evidence reference in `vectors/` and
  `gate_vectors/` is synthetic, and the entries are deliberately nonsense as
  knowledge. Truth is what `verification.evidence` and a non-submitter
  confirmation are for; see `docs/lifecycle.md`.
- **That your redaction is safe.** The kit contains five shapes. A real
  ruleset (`provenance.redaction_profile`) covers far more, the categories in
  `CONTRIBUTING.md` are prose rather than a specification, and public git
  history cannot be recalled. Passing `redaction-*` means your gate is not
  obviously broken, not that your fork is clean.
- **That your schema validation is complete.** Four cases here;
  `tests/test_schema_contract.py` proves the schema itself far more thoroughly,
  and neither is a substitute for validating against
  `schemas/knowledge-v2.schema.json` directly.
- **That your exporter produces the right bytes.** Only that it produces the
  same bytes twice.
- **Agreement on anything the specification still leaves open.** See the
  next section. The former whitespace / lowercase / normalization / sort /
  per-line-trailing cases are now pinned by vectors, because
  `docs/federation.md` states them exactly.

## The normative specification

`docs/federation.md` is normative. `reference.py` is a readable rendering of
it, kept deliberately simple, used to produce the expected values in
`vectors/`, and **not** a production path — an implementation that imports it
proves nothing by passing.

If `reference.py` and `docs/federation.md` disagree, the document wins and the
vectors are wrong. That is what `selfcheck.py` exists to make fixable: it
re-derives every expected hash from its recorded payload and every payload from
its recorded input, so a wrong vector shows up as an inconsistency rather than
becoming the standard everyone conforms to.

## Where the specification is ambiguous

`docs/federation.md` is normative. Several former gaps are now stated
there exactly; this kit pins them with vectors rather than treating them
as unpinned. A silent decision by this kit would still make the kit the
spec, so remaining gaps stay listed.

**Pinned by `docs/federation.md` and by this kit.** A fork that reads
them differently fails a vector. Do not "fix" the vector; fix the
implementation.

1. **Whitespace is exactly the six ASCII characters** U+0009, U+000A,
   U+000B, U+000C, U+000D, U+0020 (`non-ascii-whitespace-preserved`).
   NBSP and U+3000 are content, including at the edges of a string and
   inside a fingerprint. Python's `str.strip()` and `\s` consume them
   and diverge.
2. **Lowercasing is ASCII-only** (`ascii-lowercase-preserves-nonascii`).
   `İ` (U+0130), `É` and `Σ` stay as themselves; `ABC` becomes `abc`.
   Full Unicode `lower()` / `casefold()` is not the rule.
3. **No Unicode normalization form is applied** (`no-unicode-normalization`).
   NFD `e` + combining acute is not composed to `é`.
4. **Fingerprints sort byte-wise over UTF-8** (`fingerprint-byte-order`),
   not by locale collation.
5. **Trailing ASCII whitespace is stripped from every line**, then the
   whole value is ASCII-stripped (`line-trailing-ascii-whitespace` in
   rule prose, `nested-scope-line-trailing-whitespace` in nested scope
   strings). Leading whitespace on an interior line is kept.

**Still pinned by this kit as readings of remaining silence.**

6. **A lone CR is a line ending** (`line-endings-lone-cr`). Step 3 says
   "normalize line endings to LF" without enumerating them. `replace("\r\n",
   "\n")` alone leaves a bare CR intact.
7. **A fingerprint list emptied by normalization stays present as `[]`**
   (`fingerprints-all-empty`). Step 2 drops empties; it says nothing about
   dropping the resulting empty list. The alternative would make "has no usable
   fingerprints" hash like "never had fingerprints", which are different
   claims.
8. **Arrays other than `fingerprints` keep their authored order**
   (`scope-constraint-forms`). Steps 2 and 4 sort exactly two things:
   fingerprints and mapping keys. `scope.*.values` is therefore ordered data,
   and an implementation that sorts every list it sees diverges.
9. **Omitted optional keys are not defaulted in**
   (`rule-optional-keys-absent`). Implied by step 1, never stated. A struct
   with zero values will happily add `"avoidance":""`.
10. **Step 3 applies to every string in the payload**, not only to `rule.*`
    prose (`outer-whitespace-and-no-reflow`). "In every other string" is the
    plain reading, but an implementation that normalizes the rule body and
    passes `scope` through untouched is an easy mistake to make and is not
    excluded by the text.
11. **An IPv4 address from a documentation range is still an IP address**
    (`redaction-ipv4-documentation-range`). `CONTRIBUTING.md` forbids "IP
    addresses" flatly. A reasonable gate exempts RFC 5737 / RFC 3849 ranges,
    since an address that cannot route discloses nothing — and `tools/redact.py`
    on the parallel source-side-gate branch does exactly that. The kit pins the
    literal reading, and keeps a second uncontested vector (`redaction-ipv4`,
    RFC 2544 benchmarking range) that every reading must refuse, so an
    implementation can tell the two apart. If your gate exempts reserved
    ranges deliberately, run the kit with that exemption disabled or record
    this one vector as a reasoned deviation. What must not happen is the
    corpus rule being decided by whichever gate happened to ship first.

**Adjacent ambiguities this kit does not cover.**

12. **Invalid types are not hash vectors.** `docs/federation.md` step 0 is
    validate-first. `tools/canonical.py` runs the existing schema
    structural/type checks (and the numeric-version walk) before printing a
    hash or payload. It does not require the stored `content_hash` to already
    match, so a stale derived hash on a schema-valid claim can still be
    recomputed. A raw empty fingerprint string is a CLI reject (`minLength: 1`);
    kit vectors that need "normalizes to empty" use nonempty ASCII-whitespace
    strings instead. There is no hash vector for a schema-invalid type, because
    a successful `sha256:` for that input would be the wrong verdict.
13. **"near-identical `rule`"** in the duplicate-detection rule is undefined,
    so two bots will disagree about which entries are duplicate candidates.
    That is bot behaviour rather than canonicalization, so there is no vector
    for it, but it is the same class of problem.
14. **The redaction ruleset itself.** `CONTRIBUTING.md` lists categories in
    prose and `provenance.redaction_profile` versions "the ruleset", which is
    not written down anywhere yet. `gate_vectors/redaction-*` therefore test
    five *shapes* that any reading must refuse — they cannot test conformance
    to a ruleset that does not exist.

## Vector inventory

`vectors/` — canonicalization, 17 vectors. Group `anchor-scope-rule` is three
inputs that must all produce the recorded hash of `examples/valid-entry.yaml`.
The original 11 expected hashes are unchanged: those entries contain no
character the old Unicode-aware reading and the ratified ASCII reading
disagree on. New vectors pin the ratified cases; they were not produced by
blanket-regenerating hashes.

| Vector | What it pins |
|---|---|
| `anchor-valid-entry` | the recorded hash of `examples/valid-entry.yaml`; all three constraint forms |
| `anchor-metadata-mutated` | step 1: status, confidence, slug, provenance (including `redaction_profile`), lifecycle, revalidation (`verification.last_verified_at`, `verified_by`, evidence), `redaction_cleared_under` and the stored `content_hash` field are all excluded |
| `anchor-key-order-scrambled` | input mapping order and fingerprint order are irrelevant |
| `fingerprints-normalization` | step 2 in full for ASCII: case, edges, internal runs, dedup after normalization, empties, sort. Raw `""` was replaced with nonempty ASCII-whitespace-only items so the fixture is schema-valid (`minLength: 1`); they still drop, so the expected hash is unchanged |
| `fingerprints-all-empty` | a fingerprint list that normalizes to `[]` keeps its key. Inputs are nonempty ASCII-whitespace-only strings, not raw `""`, for the same schema reason; expected hash unchanged |
| `line-endings-crlf` | CRLF → LF, in rule prose and in a scope basis |
| `line-endings-lone-cr` | a bare CR is a line ending too |
| `outer-whitespace-and-no-reflow` | edges stripped everywhere, including `scope`; interiors untouched |
| `non-ascii-content` | `ensure_ascii=False`, `(",", ":")`, UTF-8 |
| `scope-constraint-forms` | `values` / `range` (bounded and half-open, `null` preserved) / `any`; non-fingerprint array order |
| `rule-optional-keys-absent` | absent optionals are not defaulted in |
| `non-ascii-whitespace-preserved` | NBSP and U+3000 are content; ASCII whitespace on the same list still collapses |
| `ascii-lowercase-preserves-nonascii` | fingerprint lowercasing is A–Z only; `İ É Σ` survive |
| `line-trailing-ascii-whitespace` | interior-line trailing ASCII whitespace in rule prose |
| `nested-scope-line-trailing-whitespace` | the same rule inside nested scope basis and values |
| `fingerprint-byte-order` | fingerprints sort byte-wise over UTF-8 |
| `no-unicode-normalization` | NFD sequences are not NFC-composed |

`gate_vectors/` — 14 vectors: 6 redaction refusals + 1 clean control,
4 schema refusals + 1 valid control, 2 export idempotence. Run
`python3 conformance/runner.py --list` for the live list.

| Vector | What it requires |
|---|---|
| `redaction-ipv4` | refuse an IPv4 address (RFC 2544 range; uncontested) |
| `redaction-ipv4-documentation-range` | refuse an IPv4 address from RFC 5737 too (contested — see ambiguity 11) |
| `redaction-internal-hostname` | refuse an internal-looking hostname |
| `redaction-absolute-user-path` | refuse a path revealing a user account |
| `redaction-email` | refuse an e-mail address, including in `provenance` |
| `redaction-credential` | refuse a credential-shaped string in an evidence note |
| `redaction-clean-control` | **accept** a clean document |
| `schema-omitted-scope-dimension` | refuse an omitted `scope` dimension |
| `schema-any-without-basis` | refuse an `any` claim with no `basis` |
| `schema-prose-as-evidence` | refuse prose in place of an evidence reference |
| `schema-verified-without-confirmation` | refuse `verified` that nobody confirmed |
| `schema-valid-control` | **accept** the agreed example document |
| `export-idempotent-unchanged-entry` | two exports of one entry are byte-identical |
| `export-idempotent-scrambled-key-order` | idempotence survives a reordered input |

## Adding or changing a vector

There is no "bless" command, on purpose: a tool that rewrites expected values
on demand is exactly how a wrong vector becomes the standard. Instead:

1. Write the vector's header (why this case exists), `id` matching the file
   name, `title`, `spec`, and the `entry` (or `document`).
2. Produce the expected values and read them:
   `python3 conformance/reference.py --payload < your-entry.yaml`.
3. Paste them in as `expected_payload` (a `|-` block scalar) and
   `expected_content_hash`.
4. Run `python3 conformance/selfcheck.py` and
   `python3 -m unittest discover -s tests`. The self-check will refuse a
   vector whose hash and payload disagree, whose input does not canonicalize to
   its payload, or that breaks its invariance group. Never blanket-regenerate
   expected hashes as a blessing step.

Changing `examples/valid-entry.yaml` changes the anchor. `selfcheck.py` will
fail until `anchor-valid-entry` is regenerated, which is the intended amount of
friction: moving the anchor invalidates every fork's stored hashes.
