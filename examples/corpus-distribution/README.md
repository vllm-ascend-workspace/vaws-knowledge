# OVPack distribution example (corpus side)

This directory is the corpus-repository half of the prebuilt-distribution
pipeline. It contains:

- `corpus/` — two sample knowledge documents in the minimal public format
  (a `#` title and a non-empty body, no frontmatter).
- `corpus-release-template.yml.tmpl` — an opt-in GitHub Actions template for
  the corpus repository (the `.tmpl` suffix keeps it out of this repo's
  corpus-YAML gates; drop the suffix when copying it into
  `.github/workflows/`). It builds a dense OVPack from the exact pushed
  commit, assembles a local release directory, verifies it, and uploads it as
  a workflow artifact. Real Release creation is commented out on purpose.
  Action references carry `<pin>` placeholders: replace them with reviewed
  revisions before enabling (this also keeps login-shaped strings out of the
  host repo's redaction gate). Pinned model/version values are read from the
  installed package via `python -m vaws_knowledge.distribution pins`.
- `sync-client-example.ps1` — the client-side sync wrapper for native
  PowerShell 5.1/7. PREPARED, NOT VERIFIED: no real Windows environment ran
  it this round.

The client half (download, verify, import, switch) lives in
`vaws_knowledge.distribution`; see `docs/distribution.md`.

Rules this example follows:

- The Git commit is the content identity; the workflow refuses to build a
  dirty or moved worktree. Checksums in the release manifest prove integrity
  and model compatibility, they never replace the Git SHA.
- The export carries precomputed dense vectors (`include_vectors=True`), and
  clients import with `vector_mode=require` so a model mismatch fails loudly
  instead of silently re-embedding the whole corpus.
- The workflow runs on CPU with the pinned FastEmbed/ONNX MiniLM
  configuration; it does not download a generation model and the embedding
  endpoint rejects generation requests.
