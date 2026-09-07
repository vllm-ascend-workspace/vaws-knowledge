# Entry lifecycle

## States

| `status` | Meaning | Returned by default |
|---|---|---|
| `unverified` | bot-passed, single observation, nobody else confirmed it | no |
| `verified` | followable evidence + non-submitter confirmation | yes |
| `stale` | was verified, but not re-verified against a recent enough environment | yes, with a warning |
| `resolved` | the underlying problem was fixed; `resolved_by` points at the fix | yes, with the fix reference |
| `deprecated` | the claim was wrong, or superseded by a better entry | no |

`confidence` is orthogonal and describes how strongly the claim is held.
`high` is only allowed on `verified` / `stale` / `resolved` — writing
confidently is not evidence.

## Two different dates

- `lifecycle.updated_at` — the entry text changed
- `verification.last_verified_at` — the claim was actually re-established on a real run

Rewording, reformatting or coordinate refinement advances the first and must not
advance the second. Collapsing these into one field is how a corpus ends up
looking maintained while being unverified.

## Staleness

The sweep compares `verification.last_verified_at` and
`verification.verified_against` against the currently supported version window.
An entry is downgraded `verified → stale` when its verified environment falls
out of that window, or when it has not been re-verified within the policy
horizon.

Downgrade to `stale` is automatic and reversible. Promotion back to `verified`
needs a new evidence reference on a current environment — it is not restored by
editing a date.

`stale` entries are still returned, because an old diagnosis is usually still
the fastest route to a root cause. They are returned labelled, so a consumer
knows not to trust the version bounds.

## Resolution instead of deletion

When a failure signature is fixed upstream, mark it `resolved` and record
`lifecycle.resolved_by` (`pull_request` / `commit` / `release`).

Deleting it would be wrong: anyone still on an affected version loses the fastest
explanation of what they are hitting, and the same symptom gets re-diagnosed
from scratch. A `resolved` entry answers both questions at once — what this is,
and what you need to move past it.

## Supersession

Use `lifecycle.supersedes` / `superseded_by` when a new entry explains the same
observation better, or when two duplicates are merged. Both entries stay in the
corpus; the superseded one drops out of default results. This keeps the
`uuid` referenced by older manifests and reports resolvable.
