# Bot test fixtures

Synthetic inputs for `tests/test_bot_*.py`. Nothing here is knowledge: every
version, SoC, driver, model name and evidence reference is invented, and the
`content_hash` values are well-formed placeholders that were **not** derived
from the payload. They exist to exercise the gates, not to be cited or copied.

| File | Exercises |
|---|---|
| `conflict-a.yaml`, `conflict-b.yaml` | overlapping coordinates, same fingerprints, contradictory explanation |
| `conflict-disjoint.yaml` | same phenomenon as `conflict-a`, but on a disjoint SoC — not a conflict |
| `dup-original.yaml`, `dup-near.yaml`, `dup-exact.yaml` | near duplicate by wording, exact duplicate by `content_hash` |
| `stale.yaml` + `policy-window.yaml` | horizon, version window and retired-SoC proposals |
| `integrity-bad.yaml`, `integrity-bad-2.yaml` | promotion bypass, bot in `verified_by`, hash shape, revision divergence |
| `malformed.yaml` | YAML that does not parse (load gate, fail closed) |
| `asserted-pairs.json` | triage-asserted contradiction input for `bot/conflicts.py` |
