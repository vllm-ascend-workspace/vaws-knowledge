# tools/ test fixtures

Synthetic inputs for `tests/test_tools_*.py`. Nothing here is knowledge and
nothing here may be cited. Every uuid, version, SoC and evidence reference is
invented.

- `valid/` — one schema-valid `layer: verified` document with a correct
  `content_hash`; must pass `validate.py` and `redact.py --check`.
- `invalid/` — one defect per file, named after the defect.
- `invalid/uuid-collision/` — two files sharing a uuid; the collision only
  shows when both are validated in one run.
- `layer-mismatch/corpus/verified/` — a `layer: unverified` file placed in a
  `verified` directory; the path is the fixture.
- `export/` — fork-side candidates (bare entry lists without `provenance` or
  `content_hash`) for `export.py`.
- `allowlist.yaml` — a run-time allowlist for `redact.py --allow-file`.

Fixtures that must contain a redaction hit (addresses, user paths, host names)
are **not** stored here. Tests assemble them at run time from parts, so no such
value is ever committed.

Regenerate `content_hash` values with `tools/canonical.py` after editing a
fixture; do not edit them by hand.
