#!/usr/bin/env python3
"""Publish a verified-only snapshot after schema/integrity and redaction gates.

    python3 sync/snapshot.py --out build/snapshot

Uses existing sync/publish.py for the manifest and snapshot digest. Never
reads or writes corpus/unverified/. An empty verified corpus is an accurately
labelled empty snapshot, not runtime or acceptance evidence.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from vaws_knowledge.sync._common import (  # noqa: E402
    EXIT_ERROR,
    EXIT_GATE,
    EXIT_OK,
    Runner,
    SyncError,
    default_runner,
    gates_summary,
    load_corpus,
    repo_root_from,
    run_gate,
)
from vaws_knowledge.sync.publish import (  # noqa: E402
    build_snapshot,
    resolve_generated_at,
    resolve_revision,
    write_snapshot,
)


def run_verified_snapshot(
    *,
    corpus_dir: pathlib.Path,
    tools_dir: pathlib.Path | None,
    out: pathlib.Path,
    runner: Runner = default_runner,
    revision: str | None = None,
    generated_at: str | None = None,
    now: bool = False,
) -> dict:
    corpus_dir = pathlib.Path(corpus_dir).resolve()
    tools_dir = pathlib.Path(tools_dir).resolve() if tools_dir is not None else None
    out = pathlib.Path(out)
    verified = corpus_dir / "verified"
    if not verified.exists():
        verified.mkdir(parents=True, exist_ok=True)

    corpus = load_corpus(corpus_dir)
    schema = run_gate(tools_dir, "validate.py", [str(verified)], runner=runner)
    redaction = run_gate(tools_dir, "redact.py", ["--check", str(verified)], runner=runner)
    gates = [schema, redaction]
    if not all(gate.ok for gate in gates):
        return {
            "status": "failed",
            "wrote": False,
            "gates": [{"name": g.name, "status": g.status} for g in gates],
            "detail": gates_summary(gates),
        }

    resolved_revision = resolve_revision(corpus_dir, revision, runner)
    stamp = resolve_generated_at(corpus_dir, generated_at, now=now, runner=runner)
    files = build_snapshot(corpus, revision=resolved_revision, generated_at=stamp)
    if any(rel.startswith("unverified/") or "/unverified/" in rel for rel in files):
        raise SyncError("refusing to publish corpus/unverified/")
    if "manifest.json" not in files:
        raise SyncError("snapshot missing manifest.json")
    import json

    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    if manifest.get("layer") != "verified":
        raise SyncError("snapshot layer is not verified")
    written = write_snapshot(files, out)
    return {
        "status": "published",
        "wrote": True,
        "revision": resolved_revision,
        "generated_at": stamp,
        "files": [str(path) for path in written],
        "gates": [{"name": g.name, "status": g.status} for g in gates],
        "entry_count": manifest.get("entry_count"),
        "snapshot_digest": manifest.get("snapshot_digest"),
        "empty": manifest.get("entry_count") == 0,
        "manifest": manifest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--repo", type=pathlib.Path, default=None)
    parser.add_argument("--corpus", type=pathlib.Path, default=None)
    parser.add_argument("--tools-dir", type=pathlib.Path, default=None)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--now", action="store_true")
    args = parser.parse_args(argv)
    repo = repo_root_from(args.repo)
    corpus_dir = pathlib.Path(args.corpus).resolve() if args.corpus else repo / "corpus"
    tools_dir = pathlib.Path(args.tools_dir).resolve() if args.tools_dir else None
    try:
        result = run_verified_snapshot(
            corpus_dir=corpus_dir,
            tools_dir=tools_dir,
            out=args.out,
            revision=args.revision,
            generated_at=args.generated_at,
            now=args.now,
        )
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if result["status"] != "published":
        print(result.get("detail") or "snapshot gates failed", file=sys.stderr)
        return EXIT_GATE
    for path in result["files"]:
        print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
