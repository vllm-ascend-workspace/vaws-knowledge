# Knowledge distribution: OVPack build, release adaptation, local version sync

Status: current

This is a package-maintainer reference. Configured clients use background shared
updates; Agents do not fill manifests, choose trust levels or sequence these
operations during ordinary knowledge lookup and capture. Released content is
reference material, not an applicability or correctness decision.

`vaws_knowledge.distribution` moves the reviewed public corpus from a fixed
Git commit onto a client machine **without recomputing document embeddings**:

```
fixed Git SHA ──build──▶ dense OVPack + build manifest ──make_release──▶ release dir
release dir ──check_and_sync──▶ staging verify ──native import──▶ atomic current.json switch
```

Git remains the content identity. The release manifest's checksums prove
integrity and model compatibility; they never replace the Git SHA.

## Pinned contract

| Pin | Value |
|---|---|
| OpenViking | 0.4.19 (`openviking-sdk` 0.1.10) |
| Embedding | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` via FastEmbed/ONNX, CPU |
| Dimensions / dtype | 384 / float32 little-endian dense vectors |
| Pack format | native OVPack, `export_ovpack(include_vectors=True)` |
| Import mode | `import_ovpack(..., vector_mode="require", on_conflict="fail")` |
| Shared parent URI | `viking://resources/shared` |

A release built for any other model/version fails with an `incompatible`
result that names the differing field — clients never fall back to re-embedding
the whole corpus. The manifest also pins the actual model/tokenizer file
checksums (`embedding.model_files`) when the build is given the model cache.

## Build side (corpus repository / CI)

`build_pack(repo=..., out_dir=..., client=..., expected_sha=...)`:

1. Verifies the worktree is a clean Git checkout of the exact commit
   (`git rev-parse HEAD`, `git status --porcelain`) and materializes the corpus
   with `git archive <sha>` — never the working tree.
2. Enforces the only public format gate: a title and a non-empty body.
3. Writes each document into a scratch build root of a live native instance
   with `processing_mode="vectors_only"` (no summarizer), waits for the queue,
   and runs `check_consistency`.
4. Exports `include_vectors=True`, then records per-file sha256, a content
   digest, the embedded pack index, builder/OpenViking/SDK versions, platform,
   and the model file checksums into `<name>-<version_id>.release.json`.

`version_id` derives from the Git SHA (`v<sha12>`), so the same commit always
produces the same shared root URI and rebuilds are idempotent.

`make_release(pack_path=..., build_manifest=..., out_dir=...)` re-validates
the manifest against the pinned contract, re-hashes the pack, and assembles:

```
<release-dir>/release.json      # the validated release manifest
<release-dir>/<pack.file>       # the dense OVPack asset
```

`publish_release(directory, repository=...)` verifies the pack and binds its
release tag to the corpus Git SHA. It uploads both assets as a draft before
publishing. Repeated publication verifies the existing release and never replaces
published assets. `GitHubReleaseSource` consumes `github://owner/repository`,
caches integrity-checked assets and verifies the source tag before importing.

## Client side (local knowledge service)

After actual knowledge use activates it, the existing lifecycle calls one function when due and
periodically while alive; this module installs no OS timer and no daemon:

```python
result = check_and_sync(
    state_root,                      # e.g. the knowledge state dir
    source,                          # local release dir, ReleaseSource, or path
    embedding_info=info,             # live endpoint identity: {model, dimension}
    client=client,                   # connected native client (or openviking_url=...)
    metrics_reader=read_metrics,     # embedding /health counters (optional)
    model_cache=cache_dir,           # verify model/tokenizer pins (optional)
    smoke_query="…",                 # optional post-import probe
)
result.status  # unchanged | switched | busy | offline | corrupt | incompatible | error
```

Behavior contract:

- **No change** returns `unchanged` quietly, before taking any lock, and never
  touches the instance.
- **Download** lands in an isolated `staging/` directory; the pack is hashed,
  the archive structure is checked without unpacking (absolute paths, `..`,
  drive letters and backslashes are rejected), the dense index contract and
  every corpus file checksum are verified against the release manifest.
- **Import** goes into an independent version URI
  `viking://resources/shared/<version_id>` with `vector_mode="require"`.
  While preparing, queries keep running on the old version. If a metrics
  reader is attached, any document embedding or generation call during the
  import window fails the switch — that is how silent re-embedding is ruled
  out. Creating the shared parent directory embeds its own directory record
  on first use; that instance bookkeeping is drained and counted as a
  separate `prepare_embedding_calls` phase, before the import window opens.
  Query-phase calls are counted separately again and are expected.
- **Switch** happens only after import + `check_consistency` + optional smoke
  query all pass: `current.json` is replaced atomically (fsync +
  `os.replace`). A crash at any earlier point leaves the old pointer intact;
  a retry resumes a consistent half-imported version instead of re-importing.
- **Cleanup** is limited to what this component owns: staging directories and
  non-active versions beyond `keep_inactive` (default 1, i.e. the previous
  version stays as a rollback window). `project`/`candidate` layers are never
  read or removed by sync.
- **Concurrency**: one active switcher per state root via an OS-managed lock
  on a persistent anchor file (`fcntl.flock` on POSIX, `msvcrt.locking` on
  Windows — both stdlib, no fork, no POSIX-only semantics). A live holder
  stays exclusive for any duration; the OS releases the lock when the holder
  exits or crashes, so there is no stale-metadata guessing, and the anchor is
  never unlinked, so an old owner's `release` cannot delete a later
  acquisition. The JSON payload in the file is diagnostic only. A concurrent
  check returns `busy` and the next period retries. No database file is
  overwritten in place, so Windows file locking is not an issue: a new
  version is a new URI subtree.

`current_shared(state_root)` returns `None` or
`{source_git_sha, root_uri, manifest_path}` — the local backend searches only
the active shared root; a missing/corrupt pointer degrades to the local
layers instead of crashing queries.

## Maintenance CLI

```
python -m vaws_knowledge.distribution current --state-root DIR
python -m vaws_knowledge.distribution check  --state-root DIR --source DIR \
    [--openviking-url URL] [--embedding-health-url URL] [--model-cache DIR] [--smoke-query TEXT]
python -m vaws_knowledge.distribution build  --repo PATH [--sha SHA] --out DIR \
    --openviking-url URL [--api-key KEY] [--embedding-health-url URL] [--model-cache DIR]
python -m vaws_knowledge.distribution make-release --pack FILE --manifest FILE --out DIR
python -m vaws_knowledge.distribution verify --release DIR
python -m vaws_knowledge.distribution pins          # the pinned contract as JSON (CI reads this)
python -m vaws_knowledge.distribution tenant-key --openviking-url URL \
    [--root-key-env OV_ROOT_KEY] [--account default] [--user-id NAME]
```

`tenant-key` provisions the tenant data user and prints the bare key on
stdout so CI can capture it; the root key only ever arrives via the
environment. Each command prints one JSON result; `check` exits 0 on
`unchanged` and `switched`, 1 otherwise, with an actionable `reason`.

## Lifecycle integration

- The local knowledge lifecycle (local instance manager) passes its live
  OpenViking URL / tenant key and its embedding endpoint identity into
  `check_and_sync`, and queries `current_shared(state_root)["root_uri"]` for
  the shared layer. The embedding endpoint should answer `GET /health` with
  `model`, `dimension` and cumulative call counters.
- The local instance now uses `auth_mode=api_key`, with root administration and
  tenant content keys kept in private state. Retrieval and distribution use the
  same tenant key.
- Dependency wiring is declared by the package. The root `vaws-knowledge
  distribution` CLI and the module entry expose the same maintenance commands.

## Historical validation scope

Verified on ARM64 macOS, CPU, this round: the logic test suite (69 tests) and
the real small-sample native chain — build from a fixed commit, release
assembly, sync into an isolated instance, no re-embedding at import, version
switch with modify/delete visibility, candidate layer preservation, and
restart. Not verified: x86-64 Windows (no environment this round), real
50k-scale distribution timing. See [publishing](publishing.md) for live network
setup and the current manual-review boundary.
