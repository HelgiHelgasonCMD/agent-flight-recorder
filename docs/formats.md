# Formats

## Generic event stream: `afr.generic-event.v1` (JSONL)

Schema: [`schemas/generic-event-v1.schema.json`](../schemas/generic-event-v1.schema.json).
Example: [`examples/events.jsonl`](../examples/events.jsonl). Import with `afr ingest FILE`.

One JSON object per line. Required fields: `schema`, `event_id` (stable, used for dedupe),
`ts` (RFC 3339), `session_id`, `agent` and `kind`.

| kind | extra required | meaning |
|---|---|---|
| `prompt` | — | start of a task attempt (opens an episode unless `episode_id` is given) |
| `tool_result` | `tool` | a tool call that did not report failure (`tool.exit_code` optional) |
| `tool_failure` | `tool`, `error` | a failed tool call; `error` is the raw error text (redacted on import) |
| `stop` | — | end of the agent's turn; `assistant_claim` is reduced to a boolean "success claimed" |
| `session_start` / `session_end` | — | session bounds |
| `verification` | `verification`, `episode_id` | objective check result: `kind` ∈ test_command/observed_rerun/ci_result/fixture_replay/external_check, `passed`, `exit_code`, `scope` ∈ task/root_cause, `claim` (for root_cause) |
| `lesson` | `lesson`, `episode_id` | a reviewed lesson (problem, symptoms, failed/successful approach, root cause, preconditions, `applies_when`, contraindications, versions, `supersedes`) |

Optional context on any record: `episode_id`, `workspace`, `repo`, `cwd`, `task_summary`,
`task_family`, `language`, `versions` (map), `git_head`.

Imported text is untrusted. It is redacted and size-limited, and never executed. A lesson record
can't self-declare `root_cause_status: verified`; without a recorded root-cause check, that is
downgraded to `supported`.

## Portable export: `experience-bundle.v1`

Schema: [`schemas/experience-bundle-v1.schema.json`](../schemas/experience-bundle-v1.schema.json)
(JSON Schema draft 2020-12). Produced by `afr export -o bundle.json` and consumed by
`afr import-bundle bundle.json`. Validation runs on both export and import.

```json
{
  "schema_version": "experience-bundle.v1",
  "export_id": "exp_…",
  "created_at": "2026-10-30T12:00:00+00:00",
  "producer": {"name": "agent-flight-recorder", "version": "0.1.0"},
  "workspace": "default",
  "redaction": {"applied": true, "rules_version": "afr-redaction.v1", "excerpts_included": false},
  "episodes": [{
    "episode_id": "ep_…",
    "task_summary": "…",
    "environment": {"repo_id": "repo_…", "revision": "<git sha>", "worktree_fingerprint": "wt_…", "os": "linux", "versions": {}},
    "observations": [{"event_id": "ev_…", "ts": "…", "excerpt": null, "signature": {"signature_id": "sig_…", "tool_category": "test", "exception_type": "…", "error_code": null, "resource_type": "module", "normalized_message": "…", "version_context": {}}}],
    "attempts": [{"event_id": "ev_…", "ts": "…", "tool_name": "Bash", "tool_category": "install", "program": "pip", "command": null, "files": [], "exit_code": null, "succeeded": true, "duration_ms": null, "signature_id": null}],
    "outcome": {"status": "verified_success", "root_cause_status": "unverified", "evidence": [{"evidence_id": "ver_…", "kind": "test_command", "scope": "task", "command": "pytest -q", "exit_code": 0, "passed": true, "ts": "…", "revision": "<git sha>", "worktree_fingerprint": "wt_…", "failure_signature_ids": ["sig_…"], "supporting_event_ids": [], "claim": null, "note": "…"}]},
    "lessons": [{"lesson_id": "les_…", "origin": "evidence_extracted", "status": "active", "problem": "…", "observed_symptoms": [], "failed_approaches": [], "successful_approach": "…", "preconditions": [], "contraindications": [], "versions": {}, "verification": {"task_status": "verified_success", "evidence_ids": ["ver_…"], "verified_at": "…"}, "source_episode_ids": ["ep_…"], "confidence_basis": "…"}],
    "provenance": {"source": "claude_code", "session_id": "…", "event_ids": ["ev_…"]}
  }]
}
```

Semantics consumers can rely on:
- `outcome.status` is `verified_*` only when the `evidence` array has a matching record;
  `claimed_success` means the agent said so and nothing verified it;
- `root_cause_status` is independent of `outcome.status`;
- `confidence_basis` describes the evidence and is not a probability;
- each lesson appears once, under its first source episode, and `invalidated` lessons are
  omitted by default;
- `command` and `excerpt` are `null` unless the producer exported with
  `--include-excerpts`, and are always redacted;
- `provenance.event_ids` is the sorted set of every source event id in the episode;
- the round trip (export → import → export) is identical apart from `export_id` and
  `created_at` (`tests/test_export.py::test_round_trip`).

No separate reference schema was supplied with the original brief, so this schema is the
project's own definition of the contract.
