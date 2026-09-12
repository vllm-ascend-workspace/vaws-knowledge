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
Re-delivery of the same content and kind reuses the record. Closed/merged PRs are recorded
and are not reopened automatically. Existing private candidates are not bulk
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
Legacy Markdown outside either directory is placed under `knowledge/` when a new
pack is built, with its bytes unchanged; a colliding legacy and typed path must
be resolved explicitly. This compatibility mapping does not certify that old
content still matches current code. Existing releases without the layout marker
retain their original verified files and vectors; their manifest identifies the
legacy files available to knowledge lookup. Experience lookup does not search
the broad shared version root. Integrity repair preserves the same separation.

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
