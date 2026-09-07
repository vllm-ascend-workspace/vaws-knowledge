"""Source-side tooling for the vaws-knowledge corpus.

- ``canonical``: the ``content_hash`` canonicalization from docs/federation.md
- ``redact``: the versioned redaction ruleset (``REDACTION_PROFILE``)
- ``validate``: schema + cross-entry validation of corpus files
- ``export``: the egress gate a contributing fork runs before proposing

Every module is importable as ``tools.<name>`` and runnable as
``python3 tools/<name>.py``.
"""
