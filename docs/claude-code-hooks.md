# Claude Code integration

Verified against the official hooks reference (<https://code.claude.com/docs/en/hooks>, fetched
2026-10-30) and Claude Code **2.1.314**. Test fixtures in `tests/fixtures/claude_code/` are the
documentation's own example payloads.

| hook event | matcher | afr event | fields read |
|---|---|---|---|
| `SessionStart` | — | `session_start` | `session_id`, `cwd`, `source`, `model` |
| `UserPromptSubmit` | — | `prompt` (opens an episode) | `session_id`, `prompt_id`, `prompt` → **hash only** |
| `PostToolUse` | `Bash\|PowerShell\|Edit\|MultiEdit\|Write\|NotebookEdit` | `tool_result` | `tool_name`, `tool_input.command` (Bash/PowerShell only), `tool_input.file_path`, `tool_response.bashEditDiff.changedFiles`, `tool_use_id`, `duration_ms` |
| `PostToolUseFailure` | all | `tool_failure` | `tool_name`, `tool_input.command`, `tool_use_id`, `error` (the `Exit code N` first line → exit code), `is_interrupt`, `duration_ms` |
| `Stop` | — | `stop` (closes the episode) | `last_assistant_message` → **boolean claim only** |
| `StopFailure` | — | `stop` | `error` (API error type) |
| `SessionEnd` | — | `session_end` | `reason` |

Common fields read: `session_id`, `cwd`, `hook_event_name`, `permission_mode`, `agent_type`.
`transcript_path` is never read.

Things that are documented and that afr relies on:
- a Bash command that exits non-zero arrives as `PostToolUseFailure` whose `error` starts with
  `Exit code N`. `PostToolUse` therefore means the tool did not report a failure. afr records
  `succeeded=true` but leaves `exit_code` empty, because no exit code is given and none is invented;
- `hookSpecificOutput.additionalContext` on `PostToolUseFailure` adds context next to the
  error. Claude Code caps it at 10,000 characters; afr caps it at 1,500;
- command hooks support `"async": true`. afr's recording handlers use it.

## Install

```bash
afr hook install --scope project --dry-run          # show the diff only
afr hook install --scope project                    # metadata capture, async recording
afr hook install --scope project --level sanitized_evidence --proactive
afr hook uninstall
```

By default afr writes `.claude/settings.local.json`, which is personal and not committed;
`--shared` writes `.claude/settings.json` instead. It keeps a backup (`*.afr-backup`), preserves
all other settings and hooks, and identifies its own handlers by their `afr … hook receive`
command. It never edits `~/.claude`.

## MCP server

```bash
claude mcp add afr -- afr mcp serve         # or add to .mcp.json:
# {"mcpServers": {"afr": {"command": "afr", "args": ["mcp", "serve"]}}}
```

Tools: `search_experience`, `get_episode_evidence`, `report_task_outcome`.

## Not verified end-to-end

The receiver, the mapping and the proactive output were tested with the documented payload
shapes, both through the in-process receiver and through the CLI. Hook delivery from a live
Claude Code session was **not** exercised in this repository's test runs, because that
would need a credentialed session. Run `afr hook install` in a scratch project, use Claude
Code normally, then check `afr doctor` and `afr episode list`.
