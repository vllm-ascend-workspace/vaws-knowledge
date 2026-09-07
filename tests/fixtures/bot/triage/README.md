# Advisory Grok adapter fixtures

Synthetic HTTP envelopes for `tests/test_bot_triage_grok.py`. Nothing here is
knowledge, and none of these files is a live provider response. The adapter
under test injects them as transport results; it must not open a network
connection or read a real API key.

YAML knowledge documents used by the tests are built at runtime from
`examples/valid-entry.yaml` so `content_hash` stays canonical. Invalid-gate
cases reuse `tests/fixtures/tools/invalid/hash-mismatch.yaml`.
