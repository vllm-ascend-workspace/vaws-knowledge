# server/ — knowledge retrieval service

One query surface over the three trust layers of [README.md](../README.md),
plus a write path that can only ever touch the local one.

```
layers.py      mount the three layers; absence is a reported state
query.py       retrieval + coordinate matching + labelling
capture.py     the write path (candidate layer only) and content_hash
mcp_server.py  stdio JSON-RPC MCP server, Content-Length framing
```

Runtime dependency: the installed `vaws-knowledge` package (PyYAML is
declared there). Nothing here imports `torch`, `torch_npu`, or touches
NPU hardware; the service reads and writes text documents.

## Layers

| Layer | Reads | Writable | Default |
|---|---|---|---|
| `shared` | packaged corpus `verified/` and `unverified/` | never | `vaws_knowledge.corpus` both subsets |
| `project` | paths from config, e.g. a business repo's `.agents/knowledge/` | never through this service | unconfigured |
| `candidate` | a developer's untracked local directory | yes, by `capture.py` | `$XDG_STATE_HOME/vaws-knowledge/candidate`, else `~/.local/state/vaws-knowledge/candidate` |

A layer is a trust source, not a status filter. Entries carry their own
`status`; default visibility is `policy.default_statuses` (`verified`,
`stale`, `resolved`). Shared `unverified` entries are therefore mounted but
hidden until a caller passes `statuses` (or changes `default_statuses`).
`VAWS_KNOWLEDGE_CORPUS` still overrides the default and resolves both
subsets from that path.

`shared` is forced read-only even if configuration asks otherwise:
[docs/federation.md](../docs/federation.md) says a fork never writes into
`verified/`. `project` is tracked content of another repo and changes through
that repo's PRs, not through a background service call.

The candidate default sits outside the checkout on purpose. A default inside
the repo eventually gets committed by somebody.

## Configuration

JSON or YAML. Precedence, lowest to highest: built-in defaults → config file →
mapping passed by an embedding caller → environment variables.

```json
{
  "layers": {
    "shared":    { "roots": ["corpus/verified"] },
    "project":   { "roots": ["../example-business-repo/.agents/knowledge"] },
    "candidate": { "root": "~/.local/state/vaws-knowledge/candidate" }
  },
  "identity": {
    "contributor": "example-handle",
    "origin_repo": "example-org/example-business-repo",
    "redaction_profile": "r1"
  },
  "policy": {
    "stale_after_days": 180,
    "default_statuses": ["verified", "stale", "resolved"]
  }
}
```

A layer may also be written as a bare string (one root), a list (several
roots), or `{"enabled": false}`. Relative roots resolve against the config
file's directory — never against the process working directory, which moves
under a long-running server.

Discovery order when no path is passed: `$VAWS_KNOWLEDGE_CONFIG`, then
`./vaws-knowledge.{json,yaml,yml}`, `./.vaws/vaws-knowledge.*`, and
`$XDG_CONFIG_HOME/vaws-knowledge/vaws-knowledge.*` (default `~/.config`).

### Environment overrides

| Variable | Effect |
|---|---|
| `VAWS_KNOWLEDGE_CORPUS` | corpus root (`verified/` + `unverified/`, or a checkout with `corpus/`) |
| `VAWS_KNOWLEDGE_CONFIG` | config file path |
| `VAWS_KNOWLEDGE_SHARED_ROOTS` | shared roots, `:`- or `,`-separated |
| `VAWS_KNOWLEDGE_PROJECT_ROOTS` | project roots, `:`- or `,`-separated |
| `VAWS_KNOWLEDGE_CANDIDATE_ROOT` | candidate root |
| `VAWS_KNOWLEDGE_LAYERS` | allowlist, e.g. `shared,project` |
| `VAWS_KNOWLEDGE_CONTRIBUTOR` / `_ORIGIN_REPO` / `_REDACTION_PROFILE` | provenance stamped by capture |
| `VAWS_KNOWLEDGE_STALE_AFTER_DAYS` | staleness labelling horizon |

Setting a `*_ROOTS` variable to the empty string disables that layer, which is
how you turn one off without editing a config file.

## Running

```bash
# resolved mounts, then exit
vaws-knowledge server --corpus /path/to/vaws-knowledge --describe

# stdio MCP server
vaws-knowledge server --corpus /path/to/vaws-knowledge [--config path/to/vaws-knowledge.json]
```

## Tools

All three tool payloads carry the same envelope: `version` (the installed
package version), `layers_available`, `layers_absent` (layer → reason),
`degraded`, `absent_fact_semantics: "unknown"`, `degradation_contract`,
`source_ref` (the installed commons commit, or `null`) and
`source_repo` (`vllm-ascend-workspace/vaws-knowledge`).

### `knowledge_query`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `text` | string | — | free-text symptom |
| `fingerprint` | string | — | matched against `rule.fingerprints` |
| `bodies` | array | `["rule","measurement"]` | restrict to one body variant |
| `reader_coordinate` | object | `{}` | your own build; any of the twelve `scope` dimensions |
| `layers` | array | `["shared","project"]` | overrides the layer set |
| `statuses` | array | policy default | replaces the default status set |
| `include_unverified` | bool | `false` | opt in to `unverified`, and to the `candidate` layer that holds it |
| `include_non_matching` | bool | `false` | also return entries whose scope excludes you, labelled |
| `kind` | string | — | restrict to one document family |
| `limit` | int | `20` | |

Each result carries `body` (`"rule"` or `"measurement"`), `layer`, `status`,
`confidence`, `content_hash`, `provenance.origin_repo`, `evidence`,
`verified_by`, `lifecycle`, `staleness`, `source`, `warnings`, `notes`, and an
`applicability` block:

```json
"applicability": {
  "applies": true,
  "covered": ["soc", "torch", "vllm", "topology"],
  "matched_on_independence_claim": [{"dimension": "cann", "basis": "..."}],
  "unchecked": ["component"],
  "undecidable": [],
  "mismatched": [],
  "dimensions": [{"dimension": "soc", "verdict": "covered", "...": "..."}]
}
```

Per-dimension verdicts:

| Verdict | Meaning |
|---|---|
| `covered` | the entry bounds the dimension and your value is inside those bounds |
| `assumed_any` | the entry claims `any`; it matches, but on an unproven claim — the `basis` is returned and a warning is attached |
| `unchecked` | you supplied no value, so nothing about this dimension was verified |
| `undecidable` | the values cannot be ordered (e.g. a non-numeric build string), or the entry's constraint is malformed |
| `mismatch` | your value is outside the entry's bounds |

An entry with any `mismatch` does not apply. It is withheld by default and the
count of withheld entries appears in `notes`; with `include_non_matching` it
comes back with `applies: false` and never outranks an entry that applies.
Ranking prefers dimensions that were *observed* (`covered`) over dimensions
that were *asserted* (`assumed_any`).

### `knowledge_capture`

| Argument | Type | Default |
|---|---|---|
| `entry` | object (schema v2 entry) | required |
| `kind` | string | `known-failure-signatures` |
| `layer` | string | `candidate` — anything else is refused |
| `dry_run` | bool | `false` |

Supply `slug`, `rule` and all twelve `scope` dimensions. `uuid`,
`content_hash`, `provenance` and the `lifecycle` dates are stamped when
absent; `status` defaults to `unverified` and `confidence` to `low`.
`rule.fingerprints` are stored in canonical form so that anyone reading the
file can reproduce `content_hash` from it.

Refusals are results, not crashes: `error: "capture_refused"` with
`refused_layer`, or `error: "capture_rejected"` with every structural problem
listed at once.

### `knowledge_explain`

`uuid` (required), plus optional `reader_coordinate` and `layers`. Returns the
entire entry — full coordinate, `verification.evidence`, the concrete
`verified_against` environment, lifecycle, conflicts — and reaches entries the
default query hides (`deprecated`, superseded). A uuid that is not present
returns `found: false` with `answer: "unknown"` and the list of layers
actually consulted.

## Default result set

Following [docs/lifecycle.md](../docs/lifecycle.md): `verified`, `stale` (with
the "do not trust its version bounds" warning) and `resolved` (with its
`resolved_by` reference) are returned. `unverified` requires
`include_unverified`. `deprecated` is never returned by default, and neither
is an entry with `lifecycle.superseded_by` set. An explicit `statuses`
argument overrides all of that, including reaching superseded entries.

## When a layer, or the service, is unavailable

The rule this preserves: **an absent fact means "unknown", never
"supported"**. So absence is always reported, never rendered as an empty
success.

| Situation | Behaviour |
|---|---|
| A layer is unconfigured | `layers_absent[layer] = "not configured (no roots supplied)"`, `degraded: true`. Not an error. |
| A configured path does not exist | `layers_absent[layer]` names the path. Other layers still answer. |
| A layer is disabled by config or env | The reason names the config key or variable. |
| No layer at all is mounted | Empty `results`, `answer: "unknown"`, and a note that every answer from this service is therefore unknown. |
| One document is malformed | Recorded in `load.errors` (layer + file relative to its root) and skipped. The service does not go down, and the gap is visible. |
| The config file cannot be parsed | The server starts with no layers, reports `configuration_error`, and answers `unknown`. |
| PyYAML is missing | JSON documents and JSON config still load; the actionable install hint appears in `warnings` and on stderr. YAML files are reported in `load.errors`. |
| A tool raises | `isError: true` with `error: "internal_error"` and `answer: "unknown"`. The loop keeps serving. |
| The whole service is unreachable | The caller sees no `initialize` response at all. Callers must treat that as unknown, not as "no known issues" — there is no in-band way for us to say it. |

`version` (the installed `vaws-knowledge` package version) appears in the
`initialize` result at top level, inside `serverInfo`, and in every tool
payload. The package version is the contract; there is no separate
service-API handshake. `measurement` is part of that contract. `bodies` is
an ordinary query filter. Rule-only fields come back as `null` rather than
absent on a measurement result, so a client can tell "not a rule" from "a
rule missing a field".

## Framing

`Content-Length: <n>\r\n\r\n<body>` over stdio, implemented in
`mcp_server.py`. The official MCP SDK is not required; if it happens to be
importable we report that in `initialize`
(`serviceInfo.official_mcp_sdk_importable`) but we do not switch transports
based on what is installed. A single-line bare JSON object is also accepted,
which makes the server drivable by hand.

## Known duplication

`capture.py` reimplements the `content_hash` canonicalization specified in
[docs/federation.md](../docs/federation.md) because the fallback must remain
usable when `tools/canonical.py` is not installed beside the server. The
implementation prefers the tool when it is present: it probes a few CLI
shapes with the entry JSON on stdin, uses the tool's hash when one comes
back, and reports a `disagreement` warning if the tool and the fallback
differ. The fallback follows the same ratified rule (ASCII whitespace and
lowercase, per-line trailing ASCII whitespace, no type coercion).
`content_hash_source` in every capture result says which implementation
produced the value.

## One thing schema v2 cannot express

The document-level `layer` field is `verified | unverified` — the two *corpus
review zones*. The three *trust layers* (`shared`, `project`, `candidate`) are
a different axis, and `tests/test_schema_contract.py` explicitly rejects
`layer: candidate` in a document. So the trust layer comes from the mount, not
from the file, and a candidate capture is written as `layer: unverified`.
Query results carry the mount-derived layer, which is the one a reader needs.
