# Public contribution and shared updates

Status: current

Local installation and retrieval preparation do not enable public uploads.
`shared_sync.enabled` defaults to true and is independent of `publishing.enabled`.
For explicitly requested contribution setup, `vaws-knowledge publishing configure --config PATH` enables
sharing according to the user's authorization and configuration. It reuses GitHub CLI authentication, creates
or reuses the user's corpus fork, and prepares a dedicated contribution clone.
Existing remotes in business repositories are not changed. Use `--read-only`
for a client that only consumes releases; downloads of the public corpus do not
require a login.

The default corpus is `vllm-ascend-workspace/vaws-knowledge-corpus`. The service
config contains `state_root`, the three layer mounts, `shared_sync`, and `publishing` settings.
Set `VAWS_KNOWLEDGE_CONFIG` to that config for MCP and CLI consumers. The workspace
provides `.agents/scripts/knowledge_setup.py` to set this up with its own paths.

With public sharing enabled, a new capture saves a private Markdown candidate and
prepares a redacted public copy in local state. The MCP service retries pending
submissions in the background, pushes the content branch to the fork, then opens
or reuses a PR. Offline or authentication failure keeps the pending record.
Re-delivery of an unchanged revision reuses the record. Document paths are stable:
knowledge has semantic entry paths and experience has persistent case IDs.
Content corrections update the same file and open PR. After a closed or merged
PR, a new revision starts a fresh branch and PR against current upstream at the
same path; unchanged content does not reopen it. Existing private candidates are not bulk
submitted when configuration is enabled.

Shared source files live in separate `knowledge/` and `experience/` directories
under the configured corpus prefix: `corpus/knowledge/` and `corpus/experience/`
in the default repository. This preserves the existing release build's
`--corpus-subdir corpus` input.
Knowledge records current conclusions that may need updating; experience records
what happened under its observed conditions, including unsuccessful attempts and
corrections. Both remain references. A historical command in an experience does
not become a current recommendation through publication.

Capture preserves the kind through redaction, the local public copy, pending
record, branch and repository path. Identical Markdown in the two kinds remains
two independent contributions. Manual preparation accepts
`vaws-knowledge contribution prepare --kind experience --candidate PATH
--state-root STATE --public-root PUBLIC`; omitting `--kind` keeps the existing
knowledge entry. `contribution submit --kind experience` selects that pending
kind; without the option it can resume either kind. Preparation never rewrites
the private source.

Capture accepts optional `ref` to correct an existing local candidate, including
its title, or `public_relpath` to select a corpus entry. For knowledge,
`knowledge/CATEGORY/ENTRY.md` creates or updates that fixed entry. For experience,
`experience/CASE.md` must already exist on the submission base. These options are
mutually exclusive; the equivalent CLI options are `--ref` and `--public-relpath`.
Contribution preparation also accepts `--public-relpath`. Capture and publishing
status return the assigned public path, without the outer `corpus/` prefix.
Different knowledge paths are independent even when their content matches.
Existing names stay unchanged; content hashes do not dictate filenames.

PR checks validate Markdown and redaction. **Human reviewers merge knowledge
PRs.** This path needs no automatic reviewer or model credential. PR preparation,
checks and publication do not prove hardware claims. The package handles this
workflow; ordinary tasks do not need a fork, publication commands or review waits.

After a corpus merge, CI builds the exact Git commit into a dense OVPack and
manifest, uploads both to a draft Release, then publishes it. The release tag
identifies the source commit. A published release is never overwritten.

New packs preserve the two directories inside their version root, for example
`viking://resources/shared/VERSION/experience/CASE.md`. The release manifest
records `content.layout: kinds/v1` and hashes the actual paths inside the pack.
Only release schema `vaws-knowledge-release/2` with `content.layout: kinds/v1`
is accepted. Schema 1 and releases without the two-store layout must be rebuilt;
there is no flat-pack query fallback. Shared queries search only the selected
kind directory under the active release and local bootstrap roots, never their
parents or individual paths selected from an old manifest.
Source Markdown outside either directory is placed under `knowledge/` when a new
pack is built, with its bytes unchanged; a colliding legacy and typed path must
be resolved explicitly. This build-time mapping does not certify that old
content still matches current code. Integrity repair preserves the same kind
directories.

While MCP is alive, its maintenance worker checks releases on startup and every 30 minutes;
failed checks retry after one minute. Submission polling is every 30 seconds.
Multiple clients share the same OS lock and state. Closing MCP ends its worker;
the next startup resumes from durable state. No OS timer or additional daemon is
installed. New packs are verified and imported before the shared pointer moves.
Failure preserves the previous shared version and all project/candidate content.
Hourly integrity checks export and compare the active content and vectors against
the retained verified pack. A missing or damaged import is restored into a new
staging namespace and activated after verification, including when the release
version is unchanged. Public contribution errors do not make local retrieval
unready. Bundled Markdown remains available alongside the current public pack.

The package's local OpenViking instance uses `api_key` auth. Root and tenant keys
are private local files with restrictive permissions; only the tenant key is
used for content operations. Capture, retrieval, build and import share this
tenant contract. Status output carries no keys.

`vaws-knowledge publishing status --config PATH` reports the last check and PRs.
`vaws-knowledge publishing once --config PATH` performs one explicit recovery
or verification pass; normal capture does not need this command.

## Experience feedback

`experience_feedback(ref, vote)` is an optional lightweight signal: `+1` if a
published case helped, `-1` if it misled the work. It requires no explanation,
new summary or candidate capture. Not using a case does not require feedback.
The CLI equivalent is `vaws-knowledge experience-feedback --ref experience/CASE.md
--vote=+1` (or `--vote=-1`). Shared experience result references are also accepted;
local candidates, knowledge and project references are not public feedback targets.

With configured sharing authorization, the tool reuses GitHub authentication,
checks the case exists in the canonical corpus, then creates or reuses its feedback
Issue and adds a [GitHub reaction](https://docs.github.com/en/rest/reactions/reactions#create-reaction-for-an-issue).
It uploads only the public case path and vote, with no session content or reason.
The response contains the Issue link and reaction counts. Issues remain separate
from source Markdown and release packs; feedback does not rewrite the case,
change retrieval ranking or promote it into maintained knowledge.

Votes belong to GitHub accounts, not tasks: retrying the same vote does not add
another, and changing one's vote replaces the prior reaction. Counts reflect
reported usefulness, not proof of correctness or applicability. GitHub remains
the feedback authority; there is no separate feedback database. Transport failure
is returned for retry without failing unrelated work. Concurrent first submissions
from different clients can create duplicate Issues because GitHub provides no
unique creation key; maintainers can consolidate those rare duplicates.

## Native-client summaries

All five workspace clients use the same knowledge and experience MCP tools. Automatic capture
uses only a native event that supplies final response text. The observed support
as of 2026-09-12 is:

| Client | Event and final-text field | Native source |
|---|---|---|
| Codex | `Stop` → `last_assistant_message` | [OpenAI hooks](https://learn.chatgpt.com/docs/hooks) |
| Claude Code | `Stop` → `last_assistant_message` | [Claude hooks](https://code.claude.com/docs/en/hooks) |
| Cursor | `afterAgentResponse` → `text` | [Cursor hooks](https://cursor.com/docs/hooks) |
| Grok | `hookEventName: "stop"` → `lastAssistantMessage` | [Grok hooks](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md) |
| Kimi Code | MCP and session support; no automatic summary capture | [Kimi hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html), [Stop implementation](https://github.com/MoonshotAI/kimi-code/blob/main/packages/agent-core-v2/src/features/externalHooks/agent/agentExternalHooksService.ts) |

Kimi Code 0.42.0 and the inspected upstream `Stop` implementation supply the
stop-hook flag without final response text. No summary hook is installed for
that event. A useful existing finding may still be captured through MCP; this
does not require an extra summary or a transcript scan.

Configured adapters accept only responses from their selected project. They
reuse final text without reading transcripts or thinking events; repeated
delivery of the same text reuses its local note. Grok's native marker prevents
its imported hooks from capturing the same event again. Empty or absent summaries
are a no-op, and optional capture errors do not interrupt the client. Lookup and
capture remain optional for every client.
Hook capture writes experience, reusing the supplied final response as the record
of that work. It does not automatically promote it to current knowledge.
Hook capture saves locally even when public publishing is disabled. Public
queuing follows the publishing setting; local persistence does not enable sharing.
Native hook trust remains managed by the client; configuration does not bypass it.

Validation depends on the tested revision and environment. Historical tests do
not establish results for another platform or a larger corpus.
