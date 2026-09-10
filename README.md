# vaws-knowledge

Local Markdown knowledge for vLLM-Ascend development, with CPU retrieval through
OpenViking, public contribution review, and prebuilt OVPack distribution.

This is the 0.3.2 development interface. Capture accepts a title and body; it no
longer requires the previous v2 YAML authoring schema. Older interfaces are not
held stable while these capabilities are being developed.

## Local knowledge

The `shared`, `project`, and `candidate` layers are returned together by
relevance and known applicability. Every result is a reference. Public review
status does not make an entry an axiom or exclude an unreviewed local observation.
Known incompatible conditions may exclude a result; unknown conditions are kept.

- Project and candidate Markdown is authoritative. The index reconciles added,
  edited, and deleted files and retries indexing pending offline captures.
- Capture writes only the local candidate directory. A failed index leaves the
  Markdown intact. `dry_run` does not alter files or start the retrieval service.
- An unavailable index returns a degraded answer rather than an authoritative
  assertion that no knowledge exists.
- Shared OVPack versions use the active root returned by the distribution
  module. Shared updates preserve project and candidate data.

Install Python 3.11 or newer and the package:

```sh
python -m pip install -e .
vaws-knowledge capture --title "Graph replay observation" \
  --content "Eager passed; graph replay differed after the input layout changed."
vaws-knowledge query --text "graph replay input layout"
vaws-knowledge server --help
```

The local instance uses OpenViking 0.4.19, SDK 0.1.10, FastEmbed 0.8.0 and the
384-dimensional multilingual MiniLM model. It runs on loopback and uses CPU
embedding. A model cache can be supplied through
`VAWS_KNOWLEDGE_EMBEDDING_CACHE`; the first uncached use downloads the model.

`VAWS_KNOWLEDGE_CONFIG` selects the layer/backend configuration.
`VAWS_KNOWLEDGE_STATE` selects local runtime state. The MCP tools are
`knowledge_query`, `knowledge_explain`, and `knowledge_capture`.

For an explicit knowledge-editing task, read the package's optional skill with
`vaws-knowledge skill`. It adds guidance on comparing conditions and conflicting
observations; ordinary query and capture need no skill. Native clients can
install the same packaged resource with
`vaws-knowledge skill --install-dir <client-skill-directory>`.
Reinstalling identical files is safe; use `--force` only to replace a differing
installed copy. No workspace checkout is required.

## Public contribution

The contribution module prepares a redacted public copy without modifying the
local source, tracks retryable submissions, and reviews the complete immutable
base-to-head Markdown change set. Grok compares candidates with retrieved public
knowledge. Missing recall, an incompatible corpus version, unsupported changes,
or a stale review cannot authorize an automatic merge.

For a genuine conflict, a human chooses a direction in the PR and Grok applies
that choice, checks the resulting changes, and proceeds through conditional
merge. The existing decision is reused while the substantive conflict remains
the same. Waiting for that choice affects only the public contribution.

```sh
vaws-knowledge contribution --help
```

Run `vaws-knowledge publishing configure --config PATH` once to reuse/create the
public corpus fork and enable submission plus shared-version sync. Only new
captures are queued; existing private candidates are not scanned for upload.
The live corpus uses format/redaction checks and **human review and merge**.
The optional Grok review module and its template remain inactive.

See [publishing setup and lifecycle](docs/publishing.md) and
[contribution usage](docs/contribution.md).

## Prebuilt distribution

The distribution module builds a dense OVPack from a fixed Git commit, records
model/tokenizer and content hashes, verifies a staged pack, imports its stored
vectors, and atomically switches the active shared version. Failure preserves
the previous active pointer. The OS holds the switch lock for the lifetime of
the operation, including imports longer than thirty minutes.

```sh
vaws-knowledge distribution --help
vaws-knowledge distribution pins
```

See [distribution usage](docs/distribution.md). Build and publish complete
GitHub Releases, or consume local directories. MCP startup and periodic checks
perform submission retries and shared synchronization in the background.
Local retrieval and OVPack import/export use the same private tenant key;
credentials are kept in local state and are excluded from status output.

## Validation scope

The development baseline is CPU Apple Silicon macOS. Regression coverage includes
capture identity, read-only dry runs, project/candidate reconciliation, contribution
review bounds, portable switch-lock ownership, and native OVPack
build/import/version-switch/restart with no document re-embedding during import.

Windows runtime verification and live xAI review are deferred. Native tests require an existing model cache;
see `tests/test_openviking_local.py` and
`tests/distribution/test_native_chain.py` for their environment variables.

The repository still contains corpus validation/redaction and v2 corpus data.
Those maintenance tools do not impose the old authoring schema or trust ranking
on the current Markdown query/capture path.
