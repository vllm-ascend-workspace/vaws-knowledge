# Public contribution and shared updates

Status: current

For requested setup, `vaws-knowledge publishing configure --config PATH` enables
sharing according to the user's authorization and configuration. It reuses GitHub CLI authentication, creates
or reuses the user's corpus fork, and prepares a dedicated contribution clone.
Existing remotes in business repositories are not changed. Use `--read-only`
for a client that only consumes releases; downloads of the public corpus do not
require a login.

The default corpus is `vllm-ascend-workspace/vaws-knowledge-corpus`. The service
config contains `state_root`, the three layer mounts, and `publishing` settings.
Set `VAWS_KNOWLEDGE_CONFIG` to that config for MCP and CLI consumers. The workspace
provides `.agents/scripts/knowledge_setup.py` to set this up with its own paths.

With public sharing enabled, a new capture saves a private Markdown candidate and
prepares a redacted public copy in local state. The MCP service retries pending
submissions in the background, pushes the content branch to the fork, then opens
or reuses a PR. Offline or authentication failure keeps the pending record.
Re-delivery of the same content reuses the record. Closed/merged PRs are recorded
and are not reopened automatically. Existing private candidates are not bulk
submitted when configuration is enabled.

PR checks validate Markdown and redaction. **Human reviewers merge knowledge
PRs.** This path needs no automatic reviewer or model credential. PR preparation,
checks and publication do not prove hardware claims. The package handles this
workflow; ordinary tasks do not need a fork, publication commands or review waits.

After a corpus merge, CI builds the exact Git commit into a dense OVPack and
manifest, uploads both to a draft Release, then publishes it. The release tag
identifies the source commit. A published release is never overwritten.

While MCP is alive, it checks new releases on startup and every 30 minutes;
failed checks retry after one minute. Submission polling is every 30 seconds.
Multiple clients share the same OS lock and state. Closing MCP ends its worker;
the next startup resumes from durable state. No OS timer or additional daemon is
installed. New packs are verified and imported before the shared pointer moves.
Failure preserves the previous shared version and all project/candidate content.

The package's local OpenViking instance uses `api_key` auth. Root and tenant keys
are private local files with restrictive permissions; only the tenant key is
used for content operations. Capture, retrieval, build and import share this
tenant contract. Status output carries no keys.

`vaws-knowledge publishing status --config PATH` reports the last check and PRs.
`vaws-knowledge publishing once --config PATH` performs one explicit recovery
or verification pass; normal capture does not need this command.

## Native-client summaries

All five workspace clients use the same knowledge MCP tools. Automatic capture
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
Hook capture saves locally even when public publishing is disabled. Public
queuing follows the publishing setting; local persistence does not enable sharing.
Native hook trust remains managed by the client; configuration does not bypass it.

Validation depends on the tested revision and environment. Historical tests do
not establish results for another platform or a larger corpus.
