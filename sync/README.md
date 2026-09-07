# sync/ — federation mechanics

Implements [docs/federation.md](../docs/federation.md). Four commands, one
shared module, no third-party dependency beyond PyYAML
(`sync/requirements.txt`). Schema validation and redaction are *not*
reimplemented here: every command shells out to `tools/validate.py` and
`tools/redact.py --check` and fails closed with `gate unavailable` (exit 2)
when they are missing.

| Command | Direction | Writes |
|---|---|---|
| `plan.py` | fork → main, dry run | nothing |
| `propose.py` | fork → main | `corpus/unverified/<kind>.yaml` (with `--apply` / `--open-pr`) |
| `publish.py` | main → forks | a snapshot directory under `--out` |
| `snapshot.py` | main → artifact | same snapshot, after schema/integrity **and** redaction gates |
| `collect.py` | public parent/forks → this repo | candidate PRs only in vaws-knowledge (`--mode propose`) |
| `rescan.py` | main-repo maintenance | a proposal JSON under `--out` |

Nothing in this package can remove an entry from the corpus. The single write
primitive (`_common.write_documents`) refuses any state in which a previously
present `uuid` is missing, and `tests/test_sync_no_delete.py` checks both that
behaviour and the absence of removal constructs in the source.

## plan.py — what would happen

```bash
python3 sync/plan.py --export fork-export.yaml [--json] [--skip-gates]
```

One line per entry, action + reason:

| action | condition | effect if proposed |
|---|---|---|
| `no-op` | same `uuid`, same `content_hash` | none |
| `new` | `uuid` unknown here | lands in `corpus/unverified/<kind>.yaml` |
| `revision` | same `uuid` in `corpus/unverified/`, different hash | same entry rewritten; `lifecycle.updated_at` advances, `verification.last_verified_at` does not |
| `duplicate-candidate` | unknown `uuid`, rule near-identical to an existing entry | reported with both uuids; not written unless `--allow-duplicate-candidates` |
| `conflict` | hash mismatch, kind mismatch, `uuid` repeated in the export, malformed id, or a revision that targets `corpus/verified/` | never written; needs a human |

Near-duplicate means any of: canonical `rule` payloads equal; normalised
summary+symptom+root_cause+resolution text ratio ≥ 0.90; fingerprint Jaccard
≥ 0.80.

## propose.py — the upward path

```bash
python3 sync/propose.py --export fork-export.yaml            # preview
python3 sync/propose.py --export fork-export.yaml --apply    # write into ./corpus
python3 sync/propose.py --export fork-export.yaml --open-pr  # PR against origin/main
```

- Per entry, never per file: touched documents are re-emitted with the entry
  upserted by `uuid` and entries sorted by `uuid`. Two forks inserting the same
  identity therefore collide on the same lines in git rather than landing twice.
- Idempotent: an all-`no-op` plan produces no branch and no PR. The branch name
  is `sync/<origin>/<proposal-id>` where the id is a digest of the applied
  `(uuid, content_hash)` pairs, so re-running the same proposal finds the open
  PR (`gh pr list --head`) and stops.
- `--open-pr` plans in a fresh `git worktree` of `<remote>/<base>`, not in the
  caller's working tree, so a race between forks is resolved by re-planning: the
  second fork's `new` degrades to `revision` or `no-op`.
- The written documents are gated again (`validate.py`, `redact.py --check`)
  before commit. `git push` is never forced.
- `--strict` exits non-zero without writing when any entry needs a human.

## collect.py — central public collection

```bash
python3 sync/collect.py --mode preview --stash /tmp/vaws-collect --json coverage.json
python3 sync/collect.py --mode propose --from-exports /tmp/vaws-collect/exports/deduped
```

Resolves scaffold parent id `1196723340`, lists accessible public forks with
bounded pagination, freezes each default-branch commit, fetches ordinary
`.agents/knowledge/*.yaml` blobs into `--stash` (outside the checkout, never
on `sys.path`), classifies v1/incomplete as unsupported, runs this
repository's `tools/export.py` plus validate/redact, deduplicates identical
uuid/hash observations, and reuses `propose.py` for preview or `--open-pr`.
Preview never writes. Conflicts are reported, not last-writer-merged.

## snapshot.py — gated verified publish

```bash
python3 sync/snapshot.py --out build/snapshot
```

Runs `tools/validate.py` and `tools/redact.py --check` on `corpus/verified/`
then `publish.py`. Refuses to emit `corpus/unverified/`.

## publish.py — the downward path

```bash
python3 sync/publish.py --out build/snapshot [--revision <sha>] [--generated-at <iso8601>]
```

Produces `<out>/verified/<kind>.yaml` (canonical key order, entries sorted by
`uuid`) and `<out>/manifest.json` with `corpus_revision`, `generated_at`,
`entry_count`, `redaction_profile_floor`, per-kind digests and a
`snapshot_digest`. Byte-reproducible for a fixed corpus + revision + timestamp.
The timestamp defaults to the committer date of the corpus revision (or
`SOURCE_DATE_EPOCH`) precisely so that the default is reproducible; `--now`
opts out. A dirty working tree is recorded as `<sha>-dirty`.

Refuses to publish if `corpus/verified/` holds an entry with status
`unverified` or a `content_hash` that does not match its payload.

## rescan.py — after the ruleset tightens

```bash
python3 sync/rescan.py --profile r2 --out rescan-r2.json [--include-findings]
```

Every entry whose `provenance.redaction_profile` is numerically below the
target is written alone to a temporary file and checked with the *current*
`tools/redact.py --check`. Failures are listed as `quarantine` items with the
remediation a maintainer must carry out (removal PR, history rewrite,
re-publish, ask the origin fork to correct and re-export). The corpus is not
modified. Tool findings are withheld from the proposal unless
`--include-findings` is given, because they tend to quote the offending text.

## Judgement calls (see PR for the reasoning)

- A revision that targets an entry in `corpus/verified/` is a `conflict`, not a
  `revision`: the claim changed, so the reviewed copy no longer covers it, and
  a fork may not write into `verified/`.
- New entries and revisions arriving with `status: verified|stale` are stored as
  `unverified` (and `confidence: high` lowered to `medium`, which the schema
  requires); the `verification` record is kept for the reviewer. Neither field
  is part of `content_hash`, so this does not create a revision.
- On a revision the fork owns `scope`, `rule`, `slug`, `confidence` and
  `provenance`; the main repo keeps `status`, `lifecycle.first_seen`,
  `supersedes`/`superseded_by`/`resolved_by`, `conflicts` and `verification`.
- `content_hash` is computed locally (`_common.content_hash`) because `sync/`
  may not import `tools/` and the CLI of `tools/canonical.py` is not fixed yet.
  It is pinned to `examples/valid-entry.yaml` by `tests/test_sync_canonical.py`
  and should be collapsed onto the tool once that exists.
