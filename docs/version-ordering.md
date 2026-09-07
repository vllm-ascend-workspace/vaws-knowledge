# Version ordering

Normative. Every implementation that evaluates a `scope` constraint must follow
this document exactly.

## Why this needs a specification

The entire premise of the structured coordinate is that a reader can
mechanically decide whether an entry applies to their build. That decision
reduces to comparing version strings against `range` bounds — and the schema
only says those bounds are strings or `null`. It says nothing about ordering.

Left unspecified, every implementation invents its own. That already happened:
three independent implementations in this repository each arrived at a different
rule before this document existed. Two of them would disagree about whether
`8.0.RC10` is newer than `8.0.RC2`, which is enough to make the same entry apply
for one reader and not for another.

## `undecidable` is a first-class result

A comparison has three outcomes, not two: **less**, **greater/equal**, and
**undecidable**.

`undecidable` must never be collapsed into either of the others, and must never
be resolved by picking the more likely reading. This is the same rule the rest of
the design follows — a missing fact is unknown rather than supported, and a
confident wrong attribution costs more than an honest gap. A reader told "this
entry may or may not apply to your CANN version, because these two version
strings are not comparable" can go and check. A reader told "applies" is not
going to check.

Consequently: if evaluating a constraint requires an undecidable comparison, the
constraint evaluates to `undecidable`, and a coordinate containing an
`undecidable` dimension is reported as `undecidable` overall — not as a match and
not as a mismatch.

## Ordering scheme per dimension

Different dimensions follow genuinely different conventions, so one universal
rule would be wrong for most of them.

| Dimension | Scheme |
|---|---|
| `torch`, `torch_npu`, `vllm`, `vllm_ascend` | PEP 440 |
| `cann`, `driver` | natural segment (below) |
| `python_abi` | exact match only — never ordered |
| `soc`, `model`, `topology`, `execution_mode`, `component` | exact match only — never ordered |

A dimension marked exact-match-only must not appear with a `range` constraint;
use `values` or `any`. An implementation encountering `range` on such a dimension
reports it as a contract violation, not as `undecidable` — the entry is malformed
rather than ambiguous.

### PEP 440

For the four Python-packaged dimensions, use PEP 440 semantics. It is a real
standard, it already handles the forms these projects actually publish
(`2.5.1.post1`, `0.11.0rc1`, `2.5.1+gitc4b1234`), and inventing a substitute for
it would be strictly worse.

Implementations should use `packaging.version` when available. When it is not
available, an implementation **must report `undecidable`** rather than falling
back to a different ordering. Silently substituting the natural-segment rule for
PEP 440 is exactly the divergence this document exists to prevent.

Local version labels (everything from `+` onward) are ignored for ordering and
retained for identity, per PEP 440.

### Natural segment

For `cann` and `driver`, whose schemes are vendor-defined and not PEP 440:

1. Discard build metadata: everything from the first `+` onward is ignored for
   ordering.
2. Split the remainder into segments on any of `.`, `-`, `_`.
3. Decompose each segment into maximal runs of ASCII digits and maximal runs of
   non-digits. `RC2` becomes `["rc", 2]`; `24` becomes `[24]`.
4. Compare run by run, left to right, case-insensitively:
   - digit run against digit run → compare as integers
   - non-digit run against non-digit run → compare as lowercase ASCII
   - digit run against non-digit run → **undecidable**
5. If one segment's runs are a proper prefix of the other's, the longer is
   greater when its remaining runs are all digit runs; otherwise
   **undecidable**.
6. Apply the same prefix rule across segments: if all shared segments are equal
   and one version has extra segments, it is greater when every extra segment
   consists only of digit runs; otherwise **undecidable**.
7. Any input that is empty, or contains no segment after step 2, is
   **undecidable** against everything.

Step 3 is the reason `8.0.RC10` sorts above `8.0.RC2`: plain lexicographic
comparison of the whole segment would order `RC10` first, which is wrong and was
one of the disagreements between existing implementations.

Step 6 is deliberately conservative. `8.0.RC2` against `8.0` is undecidable
rather than "greater", because a release candidate is not obviously newer or
older than the bare version and the vendor scheme does not tell us.

## Bounds

`range` bounds are inclusive. `null` means unbounded on that side, and is always
satisfied — but note that an unbounded side is a claim about untested territory,
which is why `docs/federation.md` asks for it to be justified in the rule body
rather than used as a default.

A `range` is satisfied when `min` compares less-or-equal to the reader's value
**and** the reader's value compares less-or-equal to `max`. If either comparison
is `undecidable`, the constraint is `undecidable`.

## `values`

`values` is set membership under exact string comparison, with one
normalization: strip surrounding whitespace. No case folding, no version
parsing. `values` is for enumerating the exact things a fact was established on,
so approximate membership would defeat its purpose.

## Worked examples

| A | B | Dimension | Result | Why |
|---|---|---|---|---|
| `8.0.RC2` | `8.0.RC3` | `cann` | A < B | runs `["rc",2]` vs `["rc",3]` |
| `8.0.RC2` | `8.0.RC10` | `cann` | A < B | digit runs compared as integers, not text |
| `8.0.RC2` | `8.1.RC1` | `cann` | A < B | decided at the second segment, `0 < 1` |
| `8.0.RC2` | `8.0` | `cann` | undecidable | extra segment is not all digits (step 6) |
| `24.1.rc3` | `24.1.rc3` | `driver` | equal | identical |
| `2.5.1` | `2.5.1.post1` | `torch` | A < B | PEP 440 post-release |
| `0.11.0rc1` | `0.11.0` | `vllm` | A < B | PEP 440 pre-release |
| `2.5.1+gitc4b1234` | `2.5.1` | `torch_npu` | equal | local label ignored for ordering |
| `0.0.0+example` | `0.1.0` | `vllm` | A < B | PEP 440 parses both |
| `ExampleSoC-A` | anything | `soc` | contract violation | `soc` is exact-match-only |

The reference fixture `examples/valid-entry.yaml` records
`verification.verified_against.cann` as `0.0.EXAMPLE`. That is not an oversight:
`EXAMPLE` decomposes to a single non-digit run, so comparing any real CANN
version against it hits step 4's digit-against-non-digit case. It is the
undecidable case, and an implementation that returns an ordering for it rather
than `undecidable` is wrong.

## Conformance

`conformance/` must carry vectors for each row of the table above, plus the
undecidable cases, so that a new implementation cannot pass while disagreeing
about ordering. An implementation that cannot evaluate PEP 440 must report
`undecidable` for those four dimensions and say so in its conformance run rather
than appearing to pass.
