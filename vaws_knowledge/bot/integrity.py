"""Id integrity checks that need the whole corpus, not one file.

What this module checks (nothing here recomputes a hash — that is
``tools/canonical.py``'s contract and the report gate shells out to it):

* one ``uuid`` never carries two different ``content_hash`` values or two
  different ``slug`` values within the same corpus snapshot,
* ``content_hash`` has the ``sha256:<64 hex>`` shape,
* an entry declares exactly one body (``rule`` or ``measurement``); neither or
  both is an error, because ``content_hash`` is defined over ``scope`` plus one
  body and cannot describe two claims under one revision,
* the directory a document lives in agrees with its ``layer`` and with the
  ``status`` of every entry in it (``corpus/verified/`` may only hold
  ``layer: verified``; an ``unverified`` layer may not hold ``verified``
  entries, which would be a promotion bypass),
* ``verification.verified_by`` for ``verified``/``stale`` entries contains a
  handle other than the submitter and never a bot handle,
* ``lifecycle.supersedes`` / ``superseded_by`` / ``conflicts[].with`` point at
  known uuids (warning only: uuids are global across forks and may legitimately
  reference an entry that lives in a project layer).

Usage::

    python3 bot/integrity.py corpus/ examples/ [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from vaws_knowledge.bot.corpus import DependencyError, EntryRef, LoadResult, load_paths, repo_root
from vaws_knowledge.bot.policy import PolicyError, load_policy

GATE_ID = "id-integrity"

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

VERIFIED_LAYER_STATUSES = frozenset({"verified", "stale", "resolved", "deprecated"})


def _finding(level: str, code: str, ref: EntryRef | None, message: str) -> dict[str, Any]:
    out: dict[str, Any] = {"level": level, "code": code, "message": message}
    if ref is not None:
        out["entry"] = ref.describe()
    return out


def _is_bot_handle(handle: str, bot_handles: Sequence[str]) -> bool:
    h = handle.strip().lower()
    return h.endswith("[bot]") or h in {b.lower() for b in bot_handles}


def check_integrity(loaded: LoadResult, policy: Mapping[str, Any]) -> dict[str, Any]:
    bot_handles = [str(h) for h in policy["integrity"]["bot_handles"]]
    findings: list[dict[str, Any]] = []
    known = {ref.uuid for ref in loaded.entries if ref.uuid}

    # -- per uuid -----------------------------------------------------------
    for uuid, refs in sorted(loaded.by_uuid().items()):
        if not uuid:
            for ref in refs:
                findings.append(_finding("error", "uuid-missing", ref, "entry has no uuid"))
            continue
        if not _UUID.match(uuid):
            for ref in refs:
                findings.append(
                    _finding("error", "uuid-shape", ref, "uuid is not a lowercase v4 uuid")
                )
        hashes = sorted({r.content_hash for r in refs})
        slugs = sorted({r.slug for r in refs})
        if len(refs) > 1:
            locations = ", ".join(r.location for r in refs)
            if len(hashes) > 1:
                findings.append(
                    _finding(
                        "error",
                        "uuid-revision-divergence",
                        refs[0],
                        f"uuid appears with {len(hashes)} different content_hash values at {locations}",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "error",
                        "uuid-duplicated",
                        refs[0],
                        f"uuid appears {len(refs)} times with the same revision at {locations}",
                    )
                )
            if len(slugs) > 1:
                findings.append(
                    _finding(
                        "error",
                        "uuid-slug-divergence",
                        refs[0],
                        f"uuid carries slugs {slugs} at {locations}",
                    )
                )

    # -- per entry ----------------------------------------------------------
    for ref in loaded.entries:
        if not _HASH.match(ref.content_hash):
            findings.append(
                _finding("error", "content-hash-shape", ref, "content_hash is not sha256:<64 hex>")
            )

        if ref.body in ("none", "both"):
            findings.append(
                _finding(
                    "error",
                    "entry-body-cardinality",
                    ref,
                    "entry declares "
                    + ("no body" if ref.body == "none" else "both a rule and a measurement")
                    + "; an entry has exactly one of rule / measurement, and "
                    "content_hash is defined over scope plus that one body",
                )
            )

        if ref.in_verified_dir and ref.layer != "verified":
            findings.append(
                _finding(
                    "error",
                    "layer-directory-mismatch",
                    ref,
                    f"document under corpus/verified/ declares layer={ref.layer or 'missing'}",
                )
            )
        if ref.in_unverified_dir and ref.layer != "unverified":
            findings.append(
                _finding(
                    "error",
                    "layer-directory-mismatch",
                    ref,
                    f"document under corpus/unverified/ declares layer={ref.layer or 'missing'}",
                )
            )
        if ref.layer == "verified" and ref.status not in VERIFIED_LAYER_STATUSES:
            findings.append(
                _finding(
                    "error",
                    "status-layer-mismatch",
                    ref,
                    f"status={ref.status or 'missing'} is not allowed in a verified-layer document",
                )
            )
        if ref.layer == "unverified" and ref.status in ("verified", "stale"):
            findings.append(
                _finding(
                    "error",
                    "promotion-bypass",
                    ref,
                    f"status={ref.status} inside an unverified-layer document; promotion "
                    "happens by moving the entry into corpus/verified/ under review",
                )
            )

        if ref.status in ("verified", "stale"):
            verified_by = ref.verification.get("verified_by")
            handles = [str(h) for h in verified_by] if isinstance(verified_by, list) else []
            submitter = str(ref.provenance.get("contributor", "")).strip().lower()
            bots = sorted(h for h in handles if _is_bot_handle(h, bot_handles))
            if bots:
                findings.append(
                    _finding(
                        "error",
                        "verified-by-bot",
                        ref,
                        f"verified_by contains bot handle(s): {', '.join(bots)}",
                    )
                )
            humans = [h for h in handles if not _is_bot_handle(h, bot_handles)]
            non_submitter = [h for h in humans if h.strip().lower() != submitter]
            if handles and not non_submitter:
                findings.append(
                    _finding(
                        "error",
                        "verified-by-submitter-only",
                        ref,
                        "verified_by contains only the submitter; verified needs a "
                        "non-submitter confirmation",
                    )
                )
            evidence = ref.verification.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                findings.append(
                    _finding(
                        "error",
                        "evidence-missing",
                        ref,
                        "verified/stale entry has no evidence reference",
                    )
                )

        lc = ref.lifecycle
        refs_out: list[tuple[str, str]] = []
        sup = lc.get("supersedes")
        if isinstance(sup, list):
            refs_out.extend(("lifecycle.supersedes", str(u)) for u in sup)
        if lc.get("superseded_by"):
            refs_out.append(("lifecycle.superseded_by", str(lc["superseded_by"])))
        for c in ref.conflicts:
            if c.get("with"):
                refs_out.append(("conflicts[].with", str(c["with"])))
        for field, target in refs_out:
            if target == ref.uuid:
                findings.append(
                    _finding("error", "self-reference", ref, f"{field} points at the entry itself")
                )
            elif target not in known:
                findings.append(
                    _finding(
                        "warning",
                        "dangling-reference",
                        ref,
                        f"{field} references {target}, which is not in the scanned corpus",
                    )
                )

    findings.sort(
        key=lambda f: (
            f["level"],
            f["code"],
            f.get("entry", {}).get("location", ""),
            f["message"],
        )
    )
    errors = [f for f in findings if f["level"] == "error"]
    return {
        "gate": GATE_ID,
        "entries_checked": len(loaded.entries),
        "findings": findings,
        "counts": {
            "errors": len(errors),
            "warnings": len(findings) - len(errors),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--policy")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        loaded = load_paths(args.paths, repo_root())
    except (DependencyError, PolicyError) as exc:
        print(f"bot/integrity: {exc}", file=sys.stderr)
        return 2
    if loaded.errors:
        for err in loaded.errors:
            print(f"bot/integrity: load error: {err.path}: {err.message}", file=sys.stderr)
        return 2
    report = check_integrity(loaded, policy)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 1 if report["counts"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
