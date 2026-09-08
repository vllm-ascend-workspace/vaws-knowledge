#!/usr/bin/env python3
"""Downward path: produce the read-only `verified` snapshot forks pull.

    python3 -m vaws_knowledge.sync.publish --out build/snapshot [--corpus corpus/]
                            [--revision <sha>] [--generated-at <iso8601>]

The snapshot is a directory:

    <out>/manifest.json               what this snapshot is (see below)
    <out>/verified/<kind>.yaml        one canonically ordered document per kind

It is reproducible: the same corpus at the same revision produces
byte-identical files. That requires the generation timestamp to be derived
from the input rather than the wall clock, so by default `generated_at` is the
committer date of the corpus revision (or $SOURCE_DATE_EPOCH when set). Pass
`--generated-at` to pin it, or `--now` to accept a non-reproducible snapshot.

manifest.json carries what a consumer needs to know what it has:

    snapshot_format          1
    schema_version           2
    layer                    "verified"
    corpus_revision          git commit of the corpus this was built from
    generated_at             ISO-8601 UTC timestamp (see above)
    entry_count              total entries across all kinds
    redaction_profile_floor  the lowest provenance.redaction_profile any
                             entry was cleared under — the weakest ruleset a
                             consumer can assume covers the whole snapshot
    redaction_profiles       histogram of profiles
    status_counts            entries per status
    kinds                    per-kind file name, entry count and sha256
    snapshot_digest          sha256 over the per-kind digests, in order

publish reads corpus/verified/ only and writes only under --out. It refuses to
publish a corpus whose verified layer contains an entry with status
`unverified`, because that would publish something nobody confirmed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import pathlib
import sys

from vaws_knowledge.sync._common import (  # noqa: E402
    EXIT_ERROR,
    EXIT_GATE,
    EXIT_OK,
    Corpus,
    IntegrityError,
    Runner,
    SyncError,
    content_hash,
    default_runner,
    dump_yaml,
    gates_summary,
    json_dumps,
    load_corpus,
    profile_number,
    repo_root_from,
    run_gate,
)

SNAPSHOT_FORMAT = 1
PUBLISHABLE_STATUSES = ("verified", "stale", "resolved", "deprecated")


def check_publishable(corpus: Corpus) -> None:
    problems = []
    for uid, loc in sorted(corpus.index.items()):
        if loc.layer != "verified":
            continue
        status = loc.entry.get("status")
        if status not in PUBLISHABLE_STATUSES:
            problems.append(f"{uid} in {loc.path} has status {status!r}")
        recorded = loc.entry.get("content_hash")
        actual = content_hash(loc.entry)
        if recorded != actual:
            problems.append(f"{uid} in {loc.path}: content_hash {recorded} != canonical {actual}")
    if problems:
        raise IntegrityError("corpus/verified/ is not publishable:\n  " + "\n  ".join(problems))


def resolve_revision(corpus_dir: pathlib.Path, override: str | None, runner: Runner) -> str:
    """The git commit the corpus was read at. A dirty working tree is recorded
    as `<sha>-dirty`: the snapshot then describes something no commit holds,
    and a consumer should be able to see that."""
    if override:
        return override
    proc = runner(["git", "-C", str(corpus_dir), "rev-parse", "HEAD"])
    if proc.returncode != 0:
        raise SyncError(
            "cannot determine the corpus revision: not a git checkout. Pass --revision."
        )
    sha = (proc.stdout or "").strip()
    status = runner(["git", "-C", str(corpus_dir), "status", "--porcelain", "--", "."])
    if status.returncode == 0 and (status.stdout or "").strip():
        print("warning: corpus working tree is dirty; recording revision as dirty", file=sys.stderr)
        sha += "-dirty"
    return sha


def resolve_generated_at(
    corpus_dir: pathlib.Path, override: str | None, *, now: bool, runner: Runner
) -> str:
    if override:
        return override
    if now:
        return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).isoformat()
    proc = runner(["git", "-C", str(corpus_dir), "log", "-1", "--format=%cI", "HEAD"])
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise SyncError(
            "cannot derive a reproducible generated_at: the corpus is not a git checkout and "
            "SOURCE_DATE_EPOCH is unset. Pass --generated-at <iso8601>, or --now to accept a "
            "non-reproducible snapshot."
        )
    stamp = _dt.datetime.fromisoformat(proc.stdout.strip())
    return stamp.astimezone(_dt.timezone.utc).isoformat()


def build_snapshot(corpus: Corpus, *, revision: str, generated_at: str) -> dict[str, bytes]:
    """Return {relative path: bytes} for the whole snapshot. Pure."""
    check_publishable(corpus)
    files: dict[str, bytes] = {}
    kinds = []
    status_counts: dict[str, int] = {}
    profiles: dict[str, int] = {}
    floor: int | None = None
    floor_label: str | None = None
    total = 0

    docs = sorted(
        (d for d in corpus.documents.values() if d.layer == "verified"), key=lambda d: d.kind
    )
    # Merge documents of the same kind (several files may share a kind).
    by_kind: dict[str, list[dict]] = {}
    for doc in docs:
        by_kind.setdefault(doc.kind, []).extend(doc.data.get("entries", []))

    for kind in sorted(by_kind):
        entries = by_kind[kind]
        document = {
            "schema_version": 2,
            "kind": kind,
            "layer": "verified",
            "updated_at": max((e.get("lifecycle", {}).get("updated_at", "") for e in entries), default=generated_at[:10]),
            "entries": entries,
        }
        content = dump_yaml(document).encode("utf-8")
        rel = f"verified/{kind}.yaml"
        files[rel] = content
        kinds.append(
            {
                "kind": kind,
                "file": rel,
                "entry_count": len(entries),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        total += len(entries)
        for e in entries:
            status_counts[e.get("status", "?")] = status_counts.get(e.get("status", "?"), 0) + 1
            profile = (e.get("provenance") or {}).get("redaction_profile", "?")
            profiles[profile] = profiles.get(profile, 0) + 1
            num = profile_number(profile)
            if num is None:
                num = -1  # unparseable profile is the weakest possible
            if floor is None or num < floor:
                floor, floor_label = num, profile

    digest = hashlib.sha256()
    for k in kinds:
        digest.update(f"{k['file']}:{k['sha256']}\n".encode())

    manifest = {
        "snapshot_format": SNAPSHOT_FORMAT,
        "schema_version": 2,
        "layer": "verified",
        "corpus_revision": revision,
        "generated_at": generated_at,
        "entry_count": total,
        "redaction_profile_floor": floor_label,
        "redaction_profiles": dict(sorted(profiles.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "kinds": kinds,
        "snapshot_digest": "sha256:" + digest.hexdigest(),
    }
    files["manifest.json"] = json_dumps(manifest).encode("utf-8")
    return files


def write_snapshot(files: dict[str, bytes], out: pathlib.Path) -> list[pathlib.Path]:
    out = pathlib.Path(out)
    written = []
    for rel in sorted(files):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[rel])
        written.append(path)
    # Stale files from an earlier publish would make the directory content
    # diverge from the manifest; report rather than delete.
    stale = sorted(
        str(p.relative_to(out))
        for p in out.rglob("*")
        if p.is_file() and str(p.relative_to(out)) not in files
    )
    if stale:
        print(
            "warning: files in the output directory are not part of this snapshot: "
            + ", ".join(stale),
            file=sys.stderr,
        )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=pathlib.Path, required=True, help="snapshot output directory")
    parser.add_argument("--repo", type=pathlib.Path, default=None)
    parser.add_argument("--corpus", type=pathlib.Path, default=None)
    parser.add_argument("--tools-dir", type=pathlib.Path, default=None)
    parser.add_argument("--revision", default=None, help="corpus revision to record (default: git HEAD)")
    parser.add_argument("--generated-at", default=None, help="ISO-8601 timestamp to record")
    parser.add_argument("--now", action="store_true", help="use the wall clock (not reproducible)")
    parser.add_argument("--skip-gates", action="store_true", help="do not run tools/validate.py on corpus/verified")
    args = parser.parse_args(argv)

    try:
        repo = repo_root_from(args.repo)
        corpus_dir = pathlib.Path(args.corpus).resolve() if args.corpus else repo / "corpus"
        tools_dir = pathlib.Path(args.tools_dir).resolve() if args.tools_dir else None
        corpus = load_corpus(corpus_dir)
        if not args.skip_gates:
            gate = run_gate(tools_dir, "validate.py", [str(corpus_dir / "verified")])
            print(gates_summary([gate]))
            if not gate.ok:
                return EXIT_GATE
        revision = resolve_revision(corpus_dir, args.revision, default_runner)
        generated_at = resolve_generated_at(corpus_dir, args.generated_at, now=args.now, runner=default_runner)
        files = build_snapshot(corpus, revision=revision, generated_at=generated_at)
        written = write_snapshot(files, args.out)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for p in written:
        print(f"wrote {p}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
