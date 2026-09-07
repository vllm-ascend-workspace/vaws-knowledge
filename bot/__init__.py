"""Review-bot gates for the vaws-knowledge commons.

The bot gates mechanical properties only: schema conformance, redaction
re-scan, duplicates, coordinate conflicts, canonical formatting and id/hash
integrity. It never decides whether a technical claim is true, so a clean bot
run only ever permits an entry into ``corpus/unverified/``.

Every module in this package is runnable both as ``python3 bot/<mod>.py`` and
as ``python3 -m bot.<mod>``, and prints machine-readable JSON with ``--json``.
``triage_grok`` is an optional advisory adapter and cannot establish truth.
``advisory_review`` wires that adapter into the trusted default-branch
workflow; it cannot approve, verify, or publish knowledge.
"""

__all__ = [
    "advisory_review",
    "conflicts",
    "corpus",
    "dedup",
    "gates",
    "integrity",
    "policy",
    "report",
    "similarity",
    "staleness",
    "triage_grok",
    "versions",
]
