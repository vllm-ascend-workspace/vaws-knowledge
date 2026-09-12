# Knowledge retrieval service

Status: current

The service reads ordinary Markdown and saves local notes. It provides optional
reference material; it does not decide whether a claim applies to the current
environment. Known conditions and evidence stay visible for the Agent to judge.
See [the package README](../../README.md) for the common usage contract.

## Agent tools

| Tool | Inputs | Result |
|---|---|---|
| `knowledge_query` | `text`; optional `limit` (default 8) | Relevant excerpts and document references |
| `knowledge_explain` | `ref` from a query | Original Markdown and recorded context |
| `knowledge_capture` | `title`, `content` | Local saved note and indexing state |
| `experience_query` | `text`; optional `limit` (default 8) | Historical case excerpts and references |
| `experience_explain` | `ref` from an experience query | Original case and recorded context |
| `experience_capture` | `title`, `content` | Local saved experience and indexing state |

Knowledge describes current conclusions; experiences preserve historical actions,
observations and outcomes. Current claims need current source and evidence review.
Historical code and commands are investigation clues, not current instructions.
Each tool reads or writes only its named store, including when resolving a ref.
The same title can exist independently in both stores. Saving into the knowledge
store does not certify correctness or freshness.

A title and non-empty body are enough. No author schema, runtime coordinate,
status choice, fixed sections or extra summary is required. Old v2 arguments
such as `entry`, `reader_coordinate`, `statuses` and `include_unverified`
are not MCP inputs. Within the selected store, retrieval returns local and shared notes by relevance,
without trust ranking, status filtering or applicability verdicts.

Responses identify unavailable storage or indexes separately from an empty
result. Neither is evidence that a claim is absent, supported or safe to ignore.
The Agent can continue independent work. MCP capture saves the Markdown without
waiting for an index or retrieval startup; the MCP worker reconciles changes later.
Queries only read the ready index and report pending maintenance when necessary.
Configured public sharing uses a separate redacted copy and follows the existing
authorization. Summary hooks save experiences locally while public sharing is disabled.

## Storage and setup

These locations are package configuration, not an author-managed lifecycle:

| Location | Content | Write path |
|---|---|---|
| `shared` | Packaged or downloaded public notes | Read-only; shared Release updates |
| `project` | A project's Markdown files | Ordinary project file editing |
| `candidate` | Local captured notes | Capture or ordinary local file editing |

Each store has these provenance layers. Their source directories and retrieval
namespaces are separate; the engine, CPU model and runtime state are shared.
Project preparation supplies `.agents/knowledge/` and `.agents/experiences/`, with
local captures in `.vaws-local/knowledge/candidate/` and
`.vaws-local/experience/candidate/`. Existing notes without an explicit kind keep
their legacy knowledge location for compatibility; this is not a current-main
validation or an automatic classification of historical content.

Shared updates preserve the project and candidate locations. There is no
promotion requirement from one location to another. Missing project configuration
does not prevent using local notes.
Preparation indexes bundled or mounted shared Markdown; no release build is
needed first. Once a prebuilt shared pack is active, its stored vectors are reused
and the matching explain tool reads its original Markdown. Both paths are internal
to the package. Bundled notes and the current pack use separate namespaces and
remain available together; obsolete pack versions are excluded from retrieval.

For a standalone configured project:

```sh
vaws-knowledge prepare --project /path/to/project
```

This creates `.vaws-local/knowledge/service.json`, downloads the CPU model as
needed, prepares the available sources, and verifies index readiness. It returns
JSON with `ready` and status, with exit code 0 for ready or 1 for pending. Offline
or partial preparation remains explicit; the MCP worker retries quietly.
Existing mounts and public contribution settings are preserved. The workspace's
dependency installation invokes this entry internally.

Storage can also be configured directly:

```json
{
  "layers": {
    "project": { "roots": ["../example-project/.agents/knowledge"] },
    "candidate": { "root": "~/.local/state/vaws-knowledge/candidate" }
  },
  "experience": {
    "layers": {
      "project": { "roots": ["../example-project/.agents/experiences"] },
      "candidate": { "root": "~/.local/state/vaws-knowledge/experience/candidate" }
    }
  }
}
```

Relative paths resolve against the configuration file's directory.
`VAWS_KNOWLEDGE_CONFIG` selects that file; `VAWS_KNOWLEDGE_STATE` selects
runtime state. Existing workspace setup can supply both without per-task inputs.
`VAWS_EXPERIENCE_PROJECT_ROOTS` and `VAWS_EXPERIENCE_CANDIDATE_ROOT`
override the experience source paths in the same way as their knowledge
counterparts. Generic service configurations without explicit experience roots
derive separate siblings at `experience/<knowledge-root-name>`.
Public corpus source keeps kinds in `corpus/knowledge/` and `corpus/experience/`;
distribution preserves both in one shared release.
Public contribution and Release setup are described in
[publishing](../../docs/publishing.md).

```sh
vaws-knowledge server --config path/to/vaws-knowledge.json --describe
vaws-knowledge server --config path/to/vaws-knowledge.json
vaws-knowledge query --text "current requirements"
vaws-knowledge experience-query --text "past graph replay failure"
vaws-knowledge experience-query --ref "EXPERIENCE_REFERENCE"
vaws-knowledge experience-capture --title "Graph replay investigation" --content "Observed results and remaining uncertainty."
```

The installed package owns the local OpenViking instance and CPU embedding.
This service performs no NPU execution. Index reconciliation and configured
submission/sync run inside the package; callers do not sequence these operations
after each capture.
Every ten seconds the worker checks local changes; hourly integrity passes
compare stored content and actual index records. Shared sync verifies the active
pack even when its release version has not changed. Missing derived content is
restored from Markdown or a verified pack. One process lock shares maintenance
across MCP connections; failed work retries without changing source notes.

## Implementation reference

`layers.py` resolves configured storage; `query.py` retrieves Markdown;
`capture.py` saves local notes; `mcp_server.py` exposes the six tools.
The package version identifies the interface.

MCP uses newline-delimited UTF-8 JSON-RPC on stdio. Only protocol messages go to
stdout. Tool errors and unavailable sources remain visible to the caller without
turning knowledge into an execution gate.
