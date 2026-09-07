# Reference fixtures

These files are contract fixtures for CI, not knowledge. Their versions, SoC
names, drivers and evidence references are synthetic and must never be cited.

`valid-entry.yaml` is the agreed shape that schema validation, redaction,
canonical hashing and query implementations all test against. If you change it,
recompute `content_hash` with the canonicalization in `docs/federation.md`
rather than editing the hash by hand.
