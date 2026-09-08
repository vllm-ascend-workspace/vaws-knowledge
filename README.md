# vaws-knowledge

Federated, evidence-gated knowledge commons for vLLM-Ascend development.

Contributing forks sink their locally verified facts here; this repo validates,
de-duplicates, resolves coordinate conflicts and publishes a shared corpus that
every fork pulls back. The point is that a failure someone already diagnosed on
Ascend should not have to be diagnosed again.

## What this repo is not

It is not a wiki, and it is not a place for advice. Every entry is a claim about
a specific coordinate in a specific environment, carrying a followable reference
to the run that established it. An entry that reads authoritative but cannot be
traced is worse than a missing entry, because it will be consumed.

## Three layers

Consumers see one query surface, backed by three layers with different trust:

| Layer | Lives in | Written by | Trust |
|---|---|---|---|
| `shared` | `corpus/verified/` in this repo | review-gated PRs only | evidence + non-submitter confirmation |
| `project` | each business repo (e.g. `.agents/knowledge/`) | that repo's PRs | tied to that repo's checkout; may be non-public |
| `candidate` | each developer's untracked local state | capture at the moment of a fix | unreviewed, single observation |

Query results are always labelled with their layer. The default result set is
`shared` + `project` at `status: verified`; anything else is opt-in.

Two reasons the `project` layer is not absorbed into this repo: some
compatibility facts are only meaningful relative to a specific submodule
checkout and would decay the moment they left it, and some facts are simply not
publishable.

## Two kinds of claim, one entry contract

An entry's body is either a `rule` or a `measurement`, and never both:

| Body | Claims | Example |
|---|---|---|
| `rule` | a failure and what to do about it — `summary`, `symptom`, `root_cause`, `resolution`, plus `fingerprints` | a container's hostname is missing from `/etc/hosts`, so gloo init fails |
| `measurement` | a quantity — a `subject` (SoC and its aliases), the `method` that established it, and `quantities` of `name` + `basis` + `value` + `unit` | `Ascend910B4` sustains 232.33 tflops on an 8192³ fp16 matmul |

Everything else is identical and equally required: `uuid`, `content_hash`, all
twelve `scope` dimensions, `provenance`, `lifecycle`, `confidence`, `status`,
`slug`, `layer`. A measurement is gated, de-duplicated, conflict-checked,
redacted, aged and promoted by exactly the same pipeline as a rule — only the
comparisons that read the body differ, and for measurements they compare
subject, quantity identity and coordinate rather than prose. Two entries
claiming a different value for the same quantity at the same coordinate are a
**conflict**, not a duplicate, and block promotion.

Measurement values are stored as strings (`"2.70336"`, not `2.70336`) for the
same reason version bounds are: `content_hash` must be byte-reproducible in
every language, and float formatting is not. Consumers parse. See
[docs/decisions.md](docs/decisions.md) §22–27 for why this is a body variant
rather than a second repository.

## Applicability coordinate

Every entry declares all twelve dimensions of `scope` — `soc`, `cann`, `driver`,
`python_abi`, `torch`, `torch_npu`, `vllm`, `vllm_ascend`, `model`, `topology`,
`execution_mode`, `component`. There is no "omitted" state. A dimension is
either bounded (`values` / `range`) or explicitly claimed independent (`any`),
and an `any` claim must state its basis.

This is the load-bearing design decision. Free-text applicability cannot be
matched against a consumer's actual checkout, so a consumer ends up applying a
result obtained on one SoC to a different one. It is also what makes conflict
resolution mechanical (below).

## Conflicts are incomplete coordinates, not disagreements

When two forks assert opposite things about what looks like the same
coordinate, that is almost never a dispute about facts. It is a dimension
nobody declared — a driver revision, a SoC difference, a topology.

So conflicts are not arbitrated. The pipeline diffs the two coordinates,
records the undeclared dimensions on both entries as a `conflicts` record, and
sends them back for refinement. Unresolved conflicts block promotion to
`verified`. This is the same confounder rule the experiment ledger uses: two
results with more than one difference cannot be attributed.

## What the review bot decides, and what it does not

The bot runs on every incoming PR and can gate on: schema conformance,
redaction re-scan, exact and near duplicates, coordinate conflicts, canonical
formatting, id and hash integrity.

The bot cannot decide whether a technical claim is true. So bot approval alone
only ever lands an entry in `corpus/unverified/`. Promotion to
`corpus/verified/` additionally requires a followable evidence reference and a
confirmation from someone who is not the submitter.

## Redaction happens at the source

Export-time redaction runs in the contributing fork, before anything reaches a
PR. The main repo re-scans as a second line of defence — not as the first one,
because public git history cannot be recalled.

The schema is the egress whitelist: `additionalProperties: false` everywhere, so
a field that is not declared cannot leave a fork. Widening any object in
`vaws_knowledge/schemas/` is a privacy decision.

See [CONTRIBUTING.md](CONTRIBUTING.md) for what must never be submitted.

## Knowledge decays

`vllm-ascend` moves, and a failure signature from two releases ago may already
be fixed. So `last_verified_at` is tracked separately from `updated_at`
(rewording is not re-verifying), entries that go too long without
re-verification are downgraded to `stale` and returned with a warning, and a
signature that gets fixed is marked `resolved` with a `resolved_by` reference
rather than deleted — deleting it strands everyone still on the affected
versions.

See [docs/lifecycle.md](docs/lifecycle.md).

## Federation

Sync is one-directional per layer: forks propose candidates upward as PRs,
this repo publishes `verified` downward. Entries sync individually, keyed by
`uuid` with `content_hash` as the revision, so re-syncing the same entry is
idempotent and a busy YAML file does not turn into a merge conflict. A
trusted central collector can also read already-public `.agents/knowledge/`
YAML from the scaffold parent and its accessible public forks and open
candidate PRs here; it never writes to a fork. See
[docs/federation.md](docs/federation.md) and
[docs/operations.md](docs/operations.md).

See [docs/federation.md](docs/federation.md).

## Install and run the engine

The engine is the `vaws-knowledge` Python package. The corpus stays a git
checkout and is **not** inside the wheel. There is no hosted service: each
user starts a local MCP server against their own corpus checkout.

```bash
uvx --from git+https://github.com/vllm-ascend-workspace/vaws-knowledge@main \
  vaws-knowledge server --corpus /path/to/vaws-knowledge
```

`--corpus` (or `VAWS_KNOWLEDGE_CORPUS`) is the corpus root: a directory that
contains `verified/`, or a full checkout that contains `corpus/verified/`.

`.mcp.json` example:

```json
{
  "mcpServers": {
    "vaws-knowledge": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vllm-ascend-workspace/vaws-knowledge@main",
        "vaws-knowledge",
        "server",
        "--corpus",
        "/path/to/vaws-knowledge"
      ]
    }
  }
}
```

Replace `@main` with a commit or tag when you want a pinned engine. The
same CLI also exposes `validate`, `redact`, `export`, `canonical`, `query`
and `conformance`. `python -m vaws_knowledge` is equivalent to
`vaws-knowledge`.

Library imports for other tools:

```python
from vaws_knowledge import canonical, validate, redact, export
```

## Layout

```
vaws_knowledge/   installable engine (canonical, validate, redact, export,
                  MCP server, review bot, sync, conformance kit, schemas)
corpus/
  verified/         shared layer — review-gated
  unverified/       bot-passed, not yet confirmed
examples/           reference entries used as CI fixtures
docs/
tests/
```

## Licensing

Code (`vaws_knowledge/`) is MIT. The corpus (`corpus/`, `examples/`)
is CC BY 4.0 — see [LICENSE-CORPUS](LICENSE-CORPUS). Contributors submit only
technical facts they are free to publish.
