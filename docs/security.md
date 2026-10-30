# Data flow, privacy and security

## What is recorded

Recording is **opt-in and project-scoped**. Nothing is captured until you run
`afr hook install` in a project, or import a file yourself. There's no background
process, keylogging or transcript scraping. `transcript_path` from hook payloads is
ignored. Chain-of-thought is never available to hooks and is never captured.

| capture level | persisted for each tool event |
|---|---|
| `metadata` (default) | tool name and category, program name (first token of a shell command), exit status, duration, a keyed hash of the normalized command, the **normalized and redacted failure signature** (exception type, error code, resource type, a headline with paths and numbers stripped), timestamps, session/episode ids, repo id (a hash of the origin URL) |
| `sanitized_evidence` | the above, plus redacted command lines (≤400 chars), redacted error excerpts (≤1,200 chars), and repo-relative file paths. Paths outside the project become keyed hashes. |

Never persisted at any level:
- prompt text (a 16-hex-char SHA-256 prefix is kept, for dedupe only);
- the final assistant message (only a boolean "success claimed" is kept);
- full `tool_input` (e.g. `Write.content`, `Edit` strings) and `tool_response` bodies;
- file contents, diffs or patches. The working tree is reduced to a hash.

## Redaction boundaries

`afr.redaction` runs at four points, and each has its own test:
1. **ingest**: before anything is written to SQLite (`test_secrets_never_reach_sqlite`, which scans the database files' bytes);
2. **indexing**: document text is redacted again before it is embedded or stored as a Qdrant payload (`test_redaction_at_index_and_retrieval_boundaries`);
3. **retrieval**: query text is redacted before use, and results come only from redacted data;
4. **export**: the bundle is redacted again before validation (`test_export_validates_and_omits_excerpts_by_default`).

Rules (`afr-redaction.v1`):
- private key blocks, Authorization/Cookie/X-API-Key headers and bearer tokens;
- Anthropic, OpenAI, GitHub, AWS, Slack, Google, Stripe and npm key formats, and JWTs;
- `user:pass@` in URLs, `*_TOKEN=`/`*SECRET*=`/`*PASSWORD*=`-style assignments, and `--password`/`--token` flags;
- long mixed-case random strings;
- the **values** of environment variables with secret-like names, plus any names listed in `AFR_REDACT_ENV`.

Redaction runs before truncation, so a cut can't split a secret and leave part of it behind.
Pure hex (git SHAs) is kept on purpose.

## Isolation and trust

- **Workspace boundary**: every search requires a workspace, and it is enforced as a Qdrant `must`
  filter before ranking. A request with no workspace raises an error. Other workspaces are visible only through an
  explicit allow-list chosen by the human operator. The MCP model cannot widen it.
- **Untrusted content**: imported and retrieved text is data. MCP responses and proactive
  hook context carry an explicit notice that they are untrusted, don't override the user's
  instructions and must not be executed automatically. afr never runs a retrieved command.
  `afr verify` runs only the command the operator typed.
- **Self-reports are not evidence**: `report_task_outcome` stores the agent's reported
  outcome separately. The objective outcome comes only from verification records.

## Reliability and observability

The hook receiver always exits 0 and writes to stdout only for a proactive note.
- SQLite lock contention is retried with backoff (4 attempts) under a 3 s busy timeout.
- If an event still can't be stored, its redacted payload goes to a bounded spool (`spool.jsonl`, 5 MB cap).
- Past the cap, events are counted as `dropped`.
- `afr doctor` reports the ingest counters (`received`/`stored`/`deduped`/`ignored`/`failed`), spool backlog, drops and index lag.
- `afr ingest --replay-spool` recovers spooled events.

Recording hooks are installed as `async` command hooks, so they never add latency to the
agent. Only the opt-in proactive failure hook is synchronous, with a 5 s hook timeout and
a 700 ms internal budget.

## Data management

Data lives in `$AFR_HOME` (default `~/.local/share/agent-flight-recorder`). Its contents:
- `afr.sqlite3`: the event store;
- `qdrant/`: embedded index, which can be rebuilt from SQLite;
- `spool.jsonl` and its stats file;
- `salt`: the keyed-hash salt, created with mode 0600.

Management commands:
- `afr episode delete <id>` removes an episode, its events and verifications, and any lesson citing only that episode;
- `afr purge --older-than N` applies retention;
- `afr export` writes a portable bundle with excerpts **excluded by default**;
- `afr hook uninstall` removes only afr's handlers.
