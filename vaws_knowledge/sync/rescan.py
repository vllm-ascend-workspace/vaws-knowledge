#!/usr/bin/env python3
"""Bulk redaction re-scan after the ruleset tightens.

    python3 sync/rescan.py --profile r2 --out rescan-r2.json [--corpus corpus/]

`provenance.redaction_profile` records which ruleset cleared each entry at
export time. When the ruleset moves to r<N+1>, every entry recorded under an
older profile is re-scanned with the *current* `tools/redact.py --check`
rather than trusted (docs/federation.md, "Re-scanning after a ruleset
change").

Each candidate entry is written on its own as a single-entry document into a
temporary directory and checked individually, so a failure is attributed to
one uuid. The result is a proposal, not a change:

    passed       entries that clear the new ruleset
    quarantine   entries that fail it — each with the layer and file it lives
                 in and the remediation the maintainer must carry out

This command never rewrites corpus/. Quarantining an entry out of
corpus/verified/ removes content from a public history, which is an explicit
main-repo operation requiring history remediation, not a commit
(docs/federation.md, "Deletion"). The proposal names the entries; a human
performs the removal.

By default the proposal does not include the redaction tool's output for
failing entries: that output tends to quote the offending text, and a report
that leaks what it found is a second incident. Pass --include-findings to
embed it, and treat the resulting file as sensitive.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
import tempfile

from vaws_knowledge.sync._common import (  # noqa: E402
    EXIT_ERROR,
    EXIT_GATE,
    EXIT_OK,
    Corpus,
    GateResult,
    Located,
    Runner,
    SyncError,
    default_runner,
    dump_yaml,
    json_dumps,
    load_corpus,
    profile_number,
    repo_root_from,
    run_gate,
    today,
)


@dataclasses.dataclass
class RescanItem:
    uuid: str
    slug: str
    kind: str
    layer: str
    path: str
    recorded_profile: str
    result: str  # passed | quarantine | gate-unavailable
    remediation: list[str] = dataclasses.field(default_factory=list)
    findings: str | None = None


@dataclasses.dataclass
class RescanReport:
    target_profile: str
    scanned_at: str
    corpus: str
    items: list[RescanItem]
    skipped_current: int  # entries already at or above the target profile

    def quarantine(self) -> list[RescanItem]:
        return [i for i in self.items if i.result == "quarantine"]

    def passed(self) -> list[RescanItem]:
        return [i for i in self.items if i.result == "passed"]

    def to_public(self) -> dict:
        return {
            "proposal": "redaction-rescan",
            "target_profile": self.target_profile,
            "scanned_at": self.scanned_at,
            "corpus": self.corpus,
            "counts": {
                "scanned": len(self.items),
                "passed": len(self.passed()),
                "quarantine": len(self.quarantine()),
                "skipped_current": self.skipped_current,
            },
            "quarantine": [dataclasses.asdict(i) for i in self.quarantine()],
            "passed": [i.uuid for i in self.passed()],
            "note": (
                "This is a proposal. Nothing in corpus/ was changed. Quarantining an entry "
                "out of corpus/verified/ is an explicit main-repo operation with history "
                "remediation; see docs/federation.md, 'Deletion'."
            ),
        }


def entries_below(corpus: Corpus, target: str) -> tuple[list[Located], int]:
    target_num = profile_number(target)
    if target_num is None:
        raise SyncError(f"--profile must look like r<N>, got {target!r}")
    below: list[Located] = []
    current = 0
    for uid in sorted(corpus.index):
        loc = corpus.index[uid]
        recorded = (loc.entry.get("provenance") or {}).get("redaction_profile")
        num = profile_number(recorded)
        if num is not None and num >= target_num:
            current += 1
            continue
        below.append(loc)
    return below, current


def _rel(corpus: Corpus, path: pathlib.Path) -> str:
    try:
        return str(pathlib.Path(path).resolve().relative_to(corpus.root.resolve().parent))
    except ValueError:
        return str(path)


def _remediation(corpus: Corpus, loc: Located, target: str) -> list[str]:
    rel = _rel(corpus, loc.path)
    steps = [
        f"remove entry {loc.entry.get('uuid')} from {rel} in a dedicated PR titled for a redaction incident",
        "rewrite history for every commit that contains the entry (public history cannot be recalled; a commit is not enough)",
        f"ask the origin fork ({(loc.entry.get('provenance') or {}).get('origin_repo', 'unknown')}) to correct and re-export under {target}",
    ]
    if loc.layer == "verified":
        steps.insert(1, "re-publish the verified snapshot so consumers stop serving the entry")
    return steps


def rescan(
    corpus: Corpus,
    target: str,
    *,
    tools_dir: pathlib.Path,
    runner: Runner = default_runner,
    include_findings: bool = False,
    day: str | None = None,
) -> RescanReport:
    below, current = entries_below(corpus, target)
    items: list[RescanItem] = []
    redact = pathlib.Path(tools_dir) / "redact.py"
    if not redact.is_file():
        raise SyncError(
            f"gate unavailable: {redact} does not exist; a re-scan without the redaction "
            "tool would trust exactly the entries it is meant to check"
        )
    with tempfile.TemporaryDirectory(prefix="vaws-rescan-") as tmp:
        tmp_dir = pathlib.Path(tmp)
        for loc in below:
            single = {
                "schema_version": 2,
                "kind": loc.kind,
                "layer": loc.layer,
                "updated_at": (loc.entry.get("lifecycle") or {}).get("updated_at", today(day)),
                "entries": [loc.entry],
            }
            probe = tmp_dir / loc.layer / loc.kind / f"{loc.entry['uuid']}.yaml"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text(dump_yaml(single), encoding="utf-8")
            gate: GateResult = run_gate(tools_dir, "redact.py", ["--check", str(probe)], runner=runner)
            item = RescanItem(
                uuid=loc.entry["uuid"],
                slug=str(loc.entry.get("slug", "")),
                kind=loc.kind,
                layer=loc.layer,
                path=_rel(corpus, loc.path),
                recorded_profile=str((loc.entry.get("provenance") or {}).get("redaction_profile")),
                result="passed" if gate.status == "passed" else "quarantine",
            )
            if gate.status == "unavailable":
                raise SyncError(gate.detail)
            if item.result == "quarantine":
                item.remediation = _remediation(corpus, loc, target)
                if include_findings:
                    item.findings = gate.detail
            items.append(item)
    return RescanReport(
        target_profile=target,
        scanned_at=today(day),
        corpus=corpus.root.name,
        items=items,
        skipped_current=current,
    )


def render_report(report: RescanReport) -> str:
    lines = [
        f"redaction re-scan under {report.target_profile}: "
        f"{len(report.items)} scanned, {len(report.passed())} passed, "
        f"{len(report.quarantine())} to quarantine, {report.skipped_current} already current",
    ]
    for item in report.quarantine():
        lines.append(f"  quarantine {item.uuid} ({item.layer}/{item.kind}, recorded {item.recorded_profile})")
        for step in item.remediation:
            lines.append(f"      - {step}")
    if report.quarantine():
        lines.append("nothing was changed; apply the remediation above explicitly")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="the new ruleset version, e.g. r2")
    parser.add_argument("--out", type=pathlib.Path, default=None, help="write the proposal JSON here")
    parser.add_argument("--repo", type=pathlib.Path, default=None)
    parser.add_argument("--corpus", type=pathlib.Path, default=None)
    parser.add_argument("--tools-dir", type=pathlib.Path, default=None)
    parser.add_argument("--today", default=None)
    parser.add_argument("--include-findings", action="store_true",
                        help="embed the redaction tool output for failing entries (sensitive)")
    args = parser.parse_args(argv)

    try:
        repo = repo_root_from(args.repo)
        corpus_dir = pathlib.Path(args.corpus).resolve() if args.corpus else repo / "corpus"
        tools_dir = pathlib.Path(args.tools_dir).resolve() if args.tools_dir else None
        corpus = load_corpus(corpus_dir)
        report = rescan(corpus, args.profile, tools_dir=tools_dir,
                        include_findings=args.include_findings, day=args.today)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_GATE if "gate unavailable" in str(exc) else EXIT_ERROR

    print(render_report(report))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json_dumps(report.to_public()), encoding="utf-8")
        print(f"wrote {args.out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
