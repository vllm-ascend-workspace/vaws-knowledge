# Gate vectors — SYNTHETIC BAD VALUES, EXPECTED TO BE REJECTED

**Every address, hostname, path, e-mail and credential-shaped string in this
directory is invented so that a conforming gate can refuse it.** None of it
came from a real machine, account or deployment. A bulk redaction scan of this
repository should treat this directory as expected-fail fixtures — not as a
leak, and not as something to remediate.

Each file starts with the same banner saying so, and `conformance/selfcheck.py`
fails if a file here is ever added without it.

## Where the bad values come from

| Class | Source | Example used here |
|---|---|---|
| IPv4 | RFC 2544 benchmarking range (IANA reserved) | `198.18.7.42` |
| IPv4 | RFC 5737 documentation ranges | `192.0.2.13` (TEST-NET-1) |
| hostname | RFC 2606 `.invalid`, which can never resolve | `npu-node-07.internal.example.invalid` |
| user path | invented account name containing "example" | `/home/example-user/work/...` |
| e-mail | RFC 2606 `.invalid` | `example.owner@example.invalid` |
| credential | the literal word EXAMPLE, repeated and zero-padded | `Authorization: Bearer EXAMPLE...000` |
| machine identifier | an all-zero machine slot, which no team numbers a box | `remote 000`, `microbenchmark_000_npu4_2026-06-03` |

There are two IPv4 vectors on purpose. A gate may reasonably exempt the RFC
5737 documentation ranges, since an address that cannot route discloses
nothing, so `redaction-ipv4` uses the RFC 2544 benchmarking range, which every
reading of `CONTRIBUTING.md` must refuse.
`redaction-ipv4-documentation-range` is marked `contested: true` and pins the
literal reading; see ambiguity 11 in `../README.md`.

The machine-identifier class is separate from `hostname` rather than a second
hostname vector, and the reason is the token shape: `remote 000` has no dot and
no domain suffix, so a gate that recognises internal hosts by looking for one
finds nothing to refuse. `tests/test_conformance_vectors.py` asserts that
difference directly, so the two vectors cannot silently converge. The same
vector also carries the **opposite** requirement, which is the harder half: the
document states NPU index, `torch_npu` version, matmul shape and timing API,
and a gate that scrubs those has destroyed the evidence that makes a measured
number checkable. Redaction removes *which machine*, not *what was done*.

`selfcheck.py` enforces this: a redaction vector must declare its offending
value, that value must actually appear in the document (otherwise the vector
tests nothing), it must have the shape of the class it claims, and it must fall
inside the reserved ranges above. A vector that used a plausible-looking real
address would fail the kit's own check.

## Structure of a vector

```yaml
id: redaction-ipv4          # matches the file name
gate: redaction             # redaction | schema | conflicts | export
expected_verdict: reject    # reject | accept, or byte-identical for export
title: ...
spec: ...                   # the rule being tested, by document and section
offending:                  # required on a redaction rejection
  class: ipv4
  value: 192.0.2.13
  location: document.entries[0].rule.symptom
  synthetic_source: RFC 5737 TEST-NET-1 documentation range
document: ...               # the whole knowledge document, as submitted
```

Schema rejections carry `violation` (which schema rule, and where) instead of
`offending`. Conflicts rejections carry `contradiction` (the quantity, and the
two values claimed for it) and their `document` holds **two or more entries**,
because a contradiction is a property of a pair rather than of a document.
Export vectors carry `runs: 2` and no expected output — the runner compares two
runs of your exporter against each other.

## Controls

`redaction-clean-control`, `schema-valid-control`,
`schema-measurement-valid-control`,
`conflicts-theoretical-and-sustained-control` and
`conflicts-measurement-disjoint-coordinate-control` must be **accepted**. They
are in the kit because a gate that refuses everything passes every rejection
vector while being useless, and that failure mode is invisible from the inside.

The two conflicts controls each rule out a different wrong implementation, and
both look like the rejection vector at a glance. `conflicts-theoretical-and-
sustained-control` claims the same quantity *name* twice with different values
and is not a conflict, because the `basis` differs — a theoretical peak and a
sustained fraction of it are different claims, and a gate keyed on the name
alone refuses it. `conflicts-measurement-disjoint-coordinate-control` claims
the same quantity *identity* twice with different values and is not a conflict
either, because the coordinates are disjoint — a gate that ignores `scope`
refuses that one.
The clean control also mentions `/etc/hosts`, `torch.distributed` and version
strings like `0.0.EXAMPLE`, so a gate that matches keywords or "a word with a
dot in it" fails it.

Each document is otherwise a valid entry: when `jsonschema` is installed,
`selfcheck.py` requires every non-schema vector's document to validate against
`schemas/knowledge-v2.schema.json`, so each vector has exactly the one defect
it declares.
