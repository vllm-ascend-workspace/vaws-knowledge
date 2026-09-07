# Contributing

## Submit only what you are free to publish

This is a public repository and its history cannot be recalled. Before an entry
leaves your fork it must pass the source-side redaction gate
(`tools/redact.py`), and it must not contain:

- IP addresses, hostnames, container names, MAC addresses
- absolute paths that reveal a user or org (`/home/<user>/…`, `/Users/…`, internal mounts)
- usernames, e-mail addresses, employee or ticket identifiers
- credentials of any kind, including partial tokens
- unreleased hardware identifiers, driver builds, or model names
- customer, project, or internal codenames

**One exemption, and only one.** Addresses from the reserved documentation
ranges are permitted: `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`,
`2001:db8::/32`, and the `example.invalid` / `example.com` domains. They are not
routable and identify nobody, so they leak nothing — and fixtures that exist to
prove the screening *works* need a rejectable value to feed it. Loopback is
likewise fine.

This exemption is stated here because the screening tool already implements it,
and a rule that disagrees with its own enforcement is worse than either
alternative: readers follow the prose, tools follow the code, and the gap is
where a real address eventually slips through. Reserved ranges other than those
listed — `198.18.0.0/15` benchmarking space, for instance — are **not** exempt,
because they do appear in real internal networks.

If a fact cannot be stated without one of these, it belongs in your repo's
`project` layer, not here. That is a supported outcome, not a failure.

Nothing above relies on a reviewer noticing. `additionalProperties: false` in
`schemas/knowledge-v2.schema.json` means an undeclared field cannot be exported
at all, and the redaction ruleset (`redaction_profile`) is versioned so the main
repo can re-scan the whole corpus when the rules tighten.

## An entry is a claim, not a note

Required for every entry:

- **Complete coordinate.** All twelve `scope` dimensions. Bound them (`values` /
  `range`) or explicitly claim independence (`any` + `basis`). An `any` claim
  without a stated basis is rejected, because unexamined independence claims are
  the usual root cause of contradictory knowledge across forks.
- **A root cause, not a symptom.** `rule.root_cause` must explain the mechanism.
  "Restarting fixed it" is not a root cause.
- **Followable evidence.** `verification.evidence` takes references —
  `run_manifest`, `pull_request`, `commit`, `ci_run`, `issue`. Prose is not
  evidence. If the run that established this is not referenceable, submit as
  `unverified` and say so.
- **The exact environment.** `verification.verified_against` records the single
  concrete environment observed, not a range. This is what makes the claim
  auditable later.

## Promotion path

```
your fork  --export (redact + validate)-->  PR  --bot-->  corpus/unverified/
corpus/unverified/  --evidence + non-submitter confirmation-->  corpus/verified/
```

The review bot gates schema, redaction, duplicates, conflicts and hash
integrity. It never decides whether your claim is true, so bot approval alone
lands in `unverified/`. `verified/` needs a reference someone can follow and a
confirmation from somebody other than you. `verification.verified_by` must not
contain the bot, and must not contain only the submitter.

## If your entry conflicts with an existing one

The bot will report the conflicting `uuid` and the dimensions neither entry
declared. Do not argue for one side. Refine the coordinate — the disagreement is
almost always a dimension both entries left as `any`. Both entries then narrow
and both remain true.

## Identity and revisions

`uuid` is the identity and never changes, including when you reword the entry.
`slug` is a human handle and may change. `content_hash` is the revision, over
the canonicalized `scope` + `rule` payload. Regenerate it with `tools/` rather
than by hand; sync is keyed on these three and a mismatch is rejected.

## Before opening a PR

```bash
python3 tools/validate.py corpus/ examples/
python3 tools/redact.py --check corpus/ examples/
python3 -m unittest discover -s tests
```

Run the validator in your fork first. The main repo's bot is for cross-entry
work — duplicates, conflicts, redaction re-scan — not for catching schema
mistakes one PR at a time.

## Private or unreachable sources

Central collection only sees accessible public forks of scaffold repository
id `1196723340`. A private clone is uninspected, not missing. Do not add
credentials to the collector. On that clone, using **this** repository's
tools (not the fork's `AGENTS.md` as instructions):

```bash
python3 tools/export.py .agents/knowledge/*.yaml \
  --origin-repo <owner/repo> \
  -o export.yaml
python3 sync/propose.py --export export.yaml
```

That is the original source-side opt-in path. v1 prose and incomplete
coordinates are not auto-converted.
