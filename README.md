# vaws-knowledge

Local Markdown reference notes for vLLM-Ascend development, with CPU retrieval
through OpenViking and optional public contribution and shared releases.

Knowledge helps the Agent reuse experience. Lookup and capture are optional:
ordinary work needs no knowledge checklist, structured form or extra completion
step. Results are references, not instructions or applicability decisions. Use
current evidence and judgment; a review or release does not prove a hardware claim.

## Read and capture

Knowledge describes current conclusions that need review when code or conditions
change. Experience records what happened, what was tried and what evidence was
obtained, including unsuccessful attempts and unresolved causes. Historical steps
are clues to investigate, not current operating guidance. Correct mistaken
interpretations in an experience without erasing the original observations.

The stores have separate directories and retrieval namespaces, using the same
OpenViking service and embedding model:

| Purpose | Query | Read | Save |
| --- | --- | --- | --- |
| Current knowledge | `knowledge_query(text, limit=8)` | `knowledge_explain(ref)` | `knowledge_capture(title, content)` |
| Historical cases | `experience_query(text, limit=8)` | `experience_explain(ref)` | `experience_capture(title, content)` |

Capturing an unambiguous matching title updates that note within the selected store.
Use optional `ref` to correct a candidate while changing its title. Knowledge uses
fixed semantic paths: optional `public_relpath="knowledge/graph/buffers.md"` creates
or updates that entry. Experiences keep a persistent case ID; an existing
`experience/CASE.md` selects a shared case for correction. Public filenames stay
fixed across edits, and content hashes only detect changes and check integrity.
A knowledge
write does not certify correctness or freshness; Agents must assess evidence and
current source before making a current claim. Related notes can link to each
other. Existing notes are not automatically moved or promoted between stores.
Untyped legacy notes remain reachable through knowledge for compatibility; this
does not assert that they have been checked against current main.
For a published case, `experience_feedback(ref, vote="+1")` or `vote="-1"`
optionally records whether it helped or misled the work. No reason is required.
The tool reuses configured GitHub sharing and records each usage feedback on an
Issue. Both positive and negative feedback accumulate, even from the same account;
they do not remove earlier feedback. Votes do not certify correctness
or alter retrieval ranking. See [publishing](docs/publishing.md#experience-feedback).

A title and non-empty Markdown body are enough. Keep known conditions, versions,
evidence and uncertainty in the prose. No frontmatter, fixed headings, runtime
coordinates, verification label or task association is required.

Install Python 3.11 or newer and the package:

```sh
python -m pip install -e .
vaws-knowledge prepare --project /path/to/project
vaws-knowledge server --config /path/to/project/.vaws-local/knowledge/service.json
```

Workspace installation calls `prepare` automatically. It prepares the CPU model,
bundled notes and indexes before reporting readiness. The MCP service maintains
them in the background. A standalone installation can use the same command;
it creates local configuration without enabling public contribution.

```sh
vaws-knowledge experience-capture --title "Graph replay observation" \
  --content "Eager passed; graph replay differed after the input layout changed."
vaws-knowledge experience-query --text "graph replay input layout"
vaws-knowledge experience-query --ref "REFERENCE_RETURNED_BY_EXPERIENCE_QUERY"
vaws-knowledge query --text "current graph replay requirements"
```

Within each store, shared, project and candidate notes are searched together by relevance. Their
location and recorded context remain visible; there is no trust tier or automatic
condition verdict. A missing or unavailable result means unknown and does not
block independent development.

Markdown files retain the original content. MCP capture saves locally without
waiting for retrieval startup or indexing. Background maintenance reconciles
added, edited and deleted files and periodically checks actual content and vectors.
Queries use the ready index without downloads or repairs. Shared updates preserve project and candidate
files. Configured summary hooks save final responses as local experiences even when public sharing is off;
sharing itself follows the publishing configuration. Reuse an existing useful
summary for capture instead of writing another one.

Bundled and configured Markdown remains searchable alongside the active shared
release; installing a smaller release does not hide the packaged notes.
A retrieved shared note can be read through
the matching `knowledge_explain(ref)` or `experience_explain(ref)` just like a local note. The package handles indexing
and active shared versions internally.

`VAWS_KNOWLEDGE_CONFIG` selects storage and backend configuration;
`VAWS_KNOWLEDGE_STATE` selects local runtime state. The local OpenViking instance
uses CPU embedding on loopback. `VAWS_KNOWLEDGE_EMBEDDING_CACHE` can supply an
existing model cache; preparation downloads an uncached model.
See [the service reference](vaws_knowledge/server/README.md) for setup details.

Project preparation uses `.agents/knowledge/` and `.agents/experiences/` for
project Markdown, and `.vaws-local/knowledge/candidate/` and
`.vaws-local/experience/candidate/` for local captures. Both use the state in
`.vaws-local/knowledge/instance/`. Existing configured roots remain authoritative;
`experience.layers` can configure experience roots independently. Generic service
configurations without experience roots derive separate sibling directories.
Public corpus source retains separate `corpus/knowledge/` and `corpus/experience/`
directories within one release.

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

Shared release synchronization is enabled by default, independently of public
upload permission. For explicitly requested contribution setup,
`vaws-knowledge publishing configure --config PATH` creates or reuses a
contribution fork. `--read-only` disables contribution and keeps release sync
without a fork or GitHub login. Existing
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
