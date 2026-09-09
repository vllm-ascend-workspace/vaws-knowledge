# Reference fixtures

These files are contract fixtures for CI, not knowledge. Their versions, SoC
names, drivers and evidence references are synthetic and must never be cited.

`valid-entry.yaml` is the agreed rule-body shape that schema validation, redaction,
canonical hashing and query implementations all test against. If you change it,
recompute `content_hash` with the canonicalization in `docs/federation.md`
rather than editing the hash by hand.

The sourced-reference body lives once, in
`corpus/unverified/sourced-references.yaml` (the packaged corpus). It is
official documentation / principle / guide with a citation and trust
assessment. It must not invent the twelve runtime coordinates or claim
hardware verification. Do not copy that entry into `examples/`: the PR gate
scans both directories, and uuid is identity across the whole scan.
