# vaws-knowledge

Local Markdown reference notes for vLLM-Ascend development, with CPU retrieval
through OpenViking and optional public contribution and shared releases.

Knowledge helps the Agent reuse experience. Lookup and capture are optional:
ordinary work needs no knowledge checklist, structured form or extra completion
step. Results are references, not instructions or applicability decisions. Use
current evidence and judgment; a review or release does not prove a hardware claim.

## Read and capture

`knowledge_query(text, limit=8)` finds related notes, `knowledge_explain(ref)`
reads the original, and `knowledge_capture(title, content)` saves a local note.
Capturing the same title updates that local note.
A title and non-empty Markdown body are enough. Keep known conditions, versions,
evidence and uncertainty in the prose. No frontmatter, fixed headings, runtime
coordinates, verification label or task association is required.

Install Python 3.11 or newer and the package:

```sh
python -m pip install -e .
vaws-knowledge capture --title "Graph replay observation" \
  --content "Eager passed; graph replay differed after the input layout changed."
vaws-knowledge query --text "graph replay input layout"
vaws-knowledge query --ref "REFERENCE_RETURNED_BY_QUERY"
```

Shared, project and candidate notes are searched together by relevance. Their
location and recorded context remain visible; there is no trust tier or automatic
condition verdict. A missing or unavailable result means unknown and does not
block independent development.

Markdown files retain the original content. MCP capture saves locally without
waiting for retrieval startup or indexing. Queries reconcile added, edited and
deleted files with the index. Shared updates preserve project and candidate
files. Configured summary hooks save locally even when public sharing is off;
sharing itself follows the publishing configuration. Reuse an existing useful
summary for capture instead of writing another one.

Bundled and configured Markdown can be queried directly without first building
a shared release. A retrieved shared note can be read through
`knowledge_explain(ref)` just like a local note. The package handles indexing
and active shared versions internally.

`VAWS_KNOWLEDGE_CONFIG` selects storage and backend configuration;
`VAWS_KNOWLEDGE_STATE` selects local runtime state. The local OpenViking instance
uses CPU embedding on loopback. `VAWS_KNOWLEDGE_EMBEDDING_CACHE` can supply an
existing model cache; the first uncached retrieval downloads the model.
See [the service reference](vaws_knowledge/server/README.md) for setup details.

## Optional maintenance

Project and local notes can be edited as ordinary Markdown. For an explicit
consolidation task, `vaws-knowledge skill` reads the optional
`curate-knowledge` guidance. It helps preserve conditions and unresolved
differences without prescribing a required workflow. Install it for native
discovery with `vaws-knowledge skill --install-dir <client-skill-directory>`.
Ordinary lookup, capture and task completion need no skill.

## Public contribution and shared updates

Public sharing follows existing authorization and configuration. The package
prepares a redacted public copy while preserving the private source, and handles
configured submission retries. The public corpus uses Markdown/redaction checks
and **human review and merge**. Local and shared observations remain reference
material regardless of publication status.

For requested setup, `vaws-knowledge publishing configure --config PATH`
creates or reuses a contribution fork and enables background shared updates.
`--read-only` consumes public releases without a fork or GitHub login. Existing
private candidates are not bulk uploaded when sharing is enabled. Ordinary
development does not need a fork, publishing commands or a wait for PR review.

See [publishing setup](docs/publishing.md) and [public contribution](docs/contribution.md).
These maintenance operations are separate from normal Agent work.
The [native-client table](docs/publishing.md#native-client-summaries) distinguishes
automatic final-response capture from MCP support; Kimi Code currently has MCP
and session support without a native final-text summary hook.

The distribution module builds dense OVPack releases from fixed Git commits and
verifies imports before switching the active shared version. Failed updates keep
the prior version. MCP handles configured retries and synchronization internally.
See [distribution](docs/distribution.md); detailed formats belong to the package,
not note authors.

## Validation

Local tests cover Markdown capture/query, index reconciliation, public redaction,
submission and prebuilt distribution. Windows coverage includes UTF-8 pipes,
cross-drive paths, process and file-lock handling. Native OpenViking and dense
distribution tests are opt-in and need the model cache described in
`tests/test_openviking_local.py` and `tests/distribution/test_native_chain.py`.
Set `VAWS_KNOWLEDGE_LIVE_OV=1` to run native OpenViking tests; the distribution
test has its own documented opt-in. Test fixtures are not hardware evidence.

The installed package version identifies the current interface. Agent tools and
the packaged reference notes use the same Markdown path. Retired structured
corpus and automated-review interfaces have been removed.
