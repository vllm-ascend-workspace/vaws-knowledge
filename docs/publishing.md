# Public contribution and shared updates

Status: current

`vaws-knowledge publishing configure --config PATH` enables the complete client
path for the configured workspace. It reuses GitHub CLI authentication, creates
or reuses the user's corpus fork, and prepares a dedicated contribution clone.
Existing remotes in business repositories are not changed. Use `--read-only`
for a client that only consumes releases; downloads of the public corpus do not
require a login.

The default corpus is `vllm-ascend-workspace/vaws-knowledge-corpus`. The service
config contains `state_root`, the three layer mounts, and `publishing` settings.
Set `VAWS_KNOWLEDGE_CONFIG` to that config for MCP and CLI consumers. The workspace
provides `.agents/scripts/knowledge_setup.py` to set this up with its own paths.

After configuration, a new capture saves a private Markdown candidate and
prepares a redacted public copy in local state. The MCP service retries pending
submissions in the background, pushes the content branch to the fork, then opens
or reuses a PR. Offline or authentication failure keeps the pending record.
Re-delivery of the same content reuses the record. Closed/merged PRs are recorded
and are not reopened automatically. Existing private candidates are not bulk
submitted when configuration is enabled.

PR checks validate Markdown and redaction. **Human reviewers merge knowledge
PRs. Grok review, automatic deduplication and automatic merging are deferred.**
The optional Grok modules are not called by this lifecycle and no xAI credential
is needed. PR preparation, checks and publication do not prove hardware claims.

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

The workspace installs observe-only final-response adapters for Codex/Claude
`Stop` and Cursor `afterAgentResponse`. They save only the final text supplied
by the client and never read complete transcripts. Empty/absent summaries are a
no-op. Clients without a final-response hook can use one ordinary capture call.
Native hook trust remains managed by the client; configuration does not bypass it.

Windows runtime verification and large-corpus distribution timing remain deferred.
