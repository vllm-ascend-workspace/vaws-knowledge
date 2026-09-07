"""Render one deterministic Markdown review comment from the gate results.

The PR workflow updates a single comment instead of posting a new one per
push, so the output must be a pure function of the gate results: no run
timestamps, no runner paths, stable ordering. The first line is a marker the
workflow greps for to find the comment it owns.

Usage::

    # run the gates and render
    python3 bot/report.py --mode pr corpus/ examples/ --markdown review.md --json review.json

    # render from a saved results file
    python3 bot/report.py --from-json review.json --markdown review.md

Exit status: 0 when the overall result is pass, 1 when any blocking gate did
not pass, 2 on bot misuse (bad arguments, missing dependency).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.corpus import DependencyError
from bot.gates import PASS, WARN, run_gates

MARKER = "<!-- vaws-knowledge-review-bot:v1 -->"
AUDIT_MARKER = "<!-- vaws-knowledge-corpus-audit:v1 -->"

STATUS_LABEL = {
    "pass": "✅ pass",
    "warn": "⚠️ warn",
    "fail": "❌ fail",
    "unavailable": "❌ unavailable (fail closed)",
    "error": "❌ error (fail closed)",
    "skipped": "❌ skipped (fail closed)",
}

TRUTH_STATEMENT = (
    "**What this bot decides:** schema conformance, redaction re-scan, exact and near "
    "duplicates, coordinate conflicts, canonical formatting, id/hash integrity.\n\n"
    "**What it does not decide:** whether any technical claim is true. A passing bot run "
    "therefore only permits entries to land in `corpus/unverified/`. Promotion to "
    "`corpus/verified/` additionally requires a followable evidence reference and a "
    "confirmation from someone who is not the submitter — neither of which a bot, "
    "including any LLM triage step, can supply."
)


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(results: Mapping[str, Any]) -> str:
    mode = results.get("mode", "pr")
    marker = AUDIT_MARKER if mode == "audit" else MARKER
    gates = list(results.get("gates", []))
    overall = results.get("overall", "fail")
    counts = results.get("counts", {})
    title = "Corpus audit" if mode == "audit" else "Review gates"

    lines: list[str] = [marker, f"## vaws-knowledge review bot — {title}", ""]
    verdict = "PASS" if overall == PASS else "FAIL"
    lines.append(
        f"**Result: {verdict}** — {counts.get('passed', 0)} passed, "
        f"{counts.get('warned', 0)} warned, {counts.get('failed', 0)} failed "
        f"of {counts.get('gates', len(gates))} gates."
    )
    lines.append("")
    if overall == PASS:
        lines.append(
            "Bot approval permits **`corpus/unverified/` only**. It does not establish "
            "truth and does not qualify an entry for `corpus/verified/`."
        )
    else:
        lines.append(
            "A blocking gate did not pass, so this change is not admissible even to "
            "`corpus/unverified/`. Gates that could not run count as failures (fail closed)."
        )
    lines.append("")
    lines.append(f"Scanned paths: {', '.join(f'`{p}`' for p in results.get('paths', [])) or '(none)'}.")
    lines.append("")

    lines += [
        "| # | Gate | Result | Blocking | Summary |",
        "|---|---|---|---|---|",
    ]
    for idx, g in enumerate(gates, start=1):
        lines.append(
            f"| {idx} | {_escape_cell(g['title'])} | {STATUS_LABEL.get(g['status'], g['status'])} | "
            f"{'yes' if g.get('blocking') else 'advisory'} | {_escape_cell(g.get('summary', ''))} |"
        )
    lines.append("")

    detailed = [g for g in gates if g.get("details")]
    if detailed:
        lines.append("### Details")
        lines.append("")
        for g in detailed:
            lines.append(f"<details><summary><b>{_escape_cell(g['title'])}</b> — {g['status']}</summary>")
            lines.append("")
            for d in g["details"]:
                lines.append(f"- {d}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    lines.append("### Scope of this bot")
    lines.append("")
    lines.append(TRUTH_STATEMENT)
    lines.append("")
    if any(g["status"] == WARN for g in gates):
        lines.append(
            "_Warnings do not block. Near duplicates are merged by humans via "
            "`lifecycle.supersedes`; coordinate conflicts are resolved by refining both "
            "entries, never by choosing one._"
        )
        lines.append("")
    lines.append(f"_Evaluated as of {results.get('as_of', 'unknown')} (UTC). Policy: `bot/policy.yaml`._")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", help="files or directories to gate")
    parser.add_argument("--mode", choices=("pr", "audit"), default="pr")
    parser.add_argument("--from-json", help="render an existing results file instead of running gates")
    parser.add_argument("--json", dest="json_out", help="write gate results JSON here")
    parser.add_argument("--markdown", help="write the rendered comment here (default: stdout)")
    parser.add_argument("--policy")
    parser.add_argument("--as-of", help="evaluation date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--asserted", help="JSON file of asserted contradictory uuid pairs")
    args = parser.parse_args(argv)

    try:
        if args.from_json:
            results = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        else:
            if not args.paths:
                parser.error("provide paths to gate, or --from-json")
            results = run_gates(
                args.paths,
                mode=args.mode,
                policy_path=args.policy,
                as_of=args.as_of,
                asserted_path=args.asserted,
            )
    except DependencyError as exc:
        print(f"bot/report: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"bot/report: {exc}", file=sys.stderr)
        return 2

    markdown = render_markdown(results)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0 if results.get("overall") == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
