# Architecture

agent-flight-recorder (afr) records what coding agents *observably* do, reconstructs task
attempts (episodes), separates failed attempts from objectively verified outcomes, keeps
evidence-bounded lessons, and serves them back through a CLI, an MCP server and an
opt-in Claude Code failure hook.

```
 Claude Code hooks ──stdin JSON──▶ afr hook receive ─┐            (fail-open; spool on error)
 generic JSONL ─────────────────▶ afr ingest ────────┤
 experience-bundle.v1 ─────────▶ afr import-bundle ──┤
                                                     ▼
                              ┌──────────── ingest pipeline ────────────┐
                              │ dedupe → capture-level policy →         │
                              │ redaction + size limits → signature     │
                              │ normalization → episode assignment →    │
                              │ observed re-run verification → lessons  │
                              └───────────────────┬─────────────────────┘
                                                  ▼
                      SQLite (WAL) — authoritative event store, Alembic-migrated
       sessions · episodes · events · failure_signatures · verifications · lessons ·
       retrieval_feedback · index_state · ingest_stats · proactive_log
                     │                                         ▲
     afr index sync  │ derived docs (re-redacted)              │ afr verify (runs the check,
     (idempotent,    ▼                                         │ records exit code, git HEAD,
      content-hash)  Qdrant collection (embedded or server)    │ working-tree fingerprint)
                     named vectors: dense (MiniLM, cosine) +   │
                     sparse bm25 (TF-saturated, Modifier.IDF)  │
                     │                                         │
                     ▼                                         │
   search: mandatory workspace filter → bm25 | dense | hybrid (Qdrant RRF)
           → applicability policy (rank-based) → hits with evidence + cautions
                     │
     ┌───────────────┼──────────────────────┬───────────────────────────┐
     ▼               ▼                      ▼                           ▼
  afr search     MCP: search_experience   PostToolUseFailure hook     afr export
                 get_episode_evidence     (opt-in, SQLite-only,        (experience-bundle.v1,
                 report_task_outcome      latency-budgeted)            schema-validated)
```

## Modules (`src/afr/`)

| module | responsibility |
|---|---|
| `domain/models.py` | Pydantic v2 records: Session, Episode, Event, ToolCall, EnvironmentFingerprint, FailureSignature, VerificationEvidence, Lesson, RetrievalFeedback |
| `persistence/` | SQLAlchemy Core tables, Alembic migrations (`migrations/versions`), WAL engine, bounded lock retry, repository (`store.py`) |
| `redaction/` | secret patterns, secret-named env values, keyed hashes, middle truncation |
| `episodes/` | tool categorization, failure-signature normalization, git/working-tree fingerprints |
| `ingest/` | adapter-neutral `RawEvent`, the pipeline, the dead-letter spool |
| `adapters/claude_code/` | hook payload mapping, fail-open receiver, project-scoped installer, proactive retrieval |
| `adapters/generic_jsonl.py` | `afr.generic-event.v1` importer |
| `lessons/` | evidence-bounded extraction, manual lessons, supersession/invalidation |
| `verification.py` | `afr verify`: runs checks, task vs root-cause scope |
| `retrieval/` | tokenizer, BM25 sparse encoder, embedding providers, documents, Qdrant index, search, policy |
| `export/bundle.py` | experience-bundle.v1 export/import + JSON Schema validation |
| `mcp_server/` | MCP stdio server (official `mcp` SDK, `MCPServer`) |
| `eval/` | offline benchmark harness, metrics, optional paired live runner |

## Key decisions

**SQLite is authoritative, Qdrant is derived.** Every event is persisted before any
indexing. `index_state` stores a content hash per document. `afr index sync` upserts only changed documents
under deterministic UUIDv5 point ids, so it is idempotent and safe to interrupt. A Qdrant outage
cannot lose canonical data: the next sync, or `afr index rebuild`, catches up, and `afr doctor`
reports the pending count. Tested in `tests/test_retrieval.py::test_qdrant_failure_does_not_lose_events`.

**Episodes are task attempts.** For Claude Code, an episode runs from a `UserPromptSubmit` to the next
`Stop`/prompt/`SessionEnd`. Generic imports may group events explicitly with `episode_id`.
Late events go into the episode whose time window covers them. Missing prompts open an
episode implicitly. Duplicates are dropped by a dedupe key: source, session, event and the
tool_use_id or a payload digest. Episodes are never merged because their text is similar.

**Failure signatures.** A signature combines tool category, exception type, error code,
resource type and a normalized headline. Paths, line/column/char numbers, addresses, UUIDs,
hex and timestamps are stripped from the headline. The version context is stored next to the
signature but isn't part of its identity, so the same error under two versions groups together
and version applicability is judged at retrieval time.

**Outcomes are evidence, not claims.** An episode moves `unknown → claimed_success` only
when the agent's final message claims success. Only the boolean is kept, never the text.
`verified_success` and `verified_failure` come only from objective evidence:
- `afr verify` runs a command and records its exit code, git HEAD, working-tree fingerprint and the linked failure signatures;
- imported CI/check records;
- an *observed re-run*: a command that failed earlier in the same episode later succeeds, as reported by the agent runtime.

Task verification and root-cause verification are separate fields. Passing tests never
upgrade a root cause. A root cause becomes `verified` only through a `--scope root-cause`
check that names the explanation it discriminates. A reviewer can mark it `supported`,
but not `verified`.

**Lessons never invent fixes.** Automatic extraction restates observed actions only:
- *failed approaches* are the actions taken between two occurrences of the same signature;
- the *successful approach* is the actions taken after the last failure, and is filled in only when a passing verification follows.

At the `metadata` capture level the actions aren't visible, so `successful_approach` stays
`null` with a reason. Reviewed lessons (`afr lesson add`, or `lesson` records in JSONL)
supersede the automatic draft. Lessons carry provenance (`source_episode_ids`,
`verification_ids`, `signature_ids`), a `confidence_basis` that describes evidence quality
rather than a probability, and a `superseded`/`invalidated` status.

**Hybrid retrieval with rank-only fusion.** BM25 is real: document sparse vectors hold
saturated term frequencies `tf·(k1+1)/(tf+k1·(1−b+b·dl/avgdl))`, and Qdrant's `Modifier.IDF`
multiplies in IDF from live collection statistics. Dense retrieval uses
`all-MiniLM-L6-v2` on CPU. `hybrid` runs both as Qdrant prefetches and fuses them with
server-side reciprocal rank fusion. The three modes run in one `query_batch_points` call, so
each hit reports its rank under each retriever. Raw BM25 and cosine scores are never compared.
See [why hybrid](../README.md#why-hybrid-retrieval).

**Applicability policy (rank-based).** Each candidate gets cautions from its evidence and
the caller's context:
- tier 1: `stale`, `other_project`;
- tier 2: `unverified`, `candidate`, `failed_attempt`, `no_lesson`, `different_error`, `no_fix_recorded`;
- tier 3: `precondition_mismatch` (e.g. `applies_when.os`), `version_mismatch` (major version differs), `superseded`, `contradicted`.

The tier becomes a bounded rank penalty (+0/+1/+3/+6 positions), and the final order is
`fused_rank + penalty`. Sorting by tier first would let an irrelevant but verified record
outrank a relevant one that carries a caution. The first benchmark run showed exactly that,
so the penalty is bounded.

**Workspace boundary is a pre-retrieval constraint.** `build_filter` refuses a request
without a workspace. The workspace is applied as a Qdrant `must` filter inside every prefetch
and the fusion query. Other workspaces are reachable only through an explicit
`--allow-workspace` list, set by the CLI user or at MCP server start, never by the model.
The proactive hook and `get_episode_evidence` enforce the same boundary.

**Proactive retrieval stays out of the hot path's heavy dependencies.** The PostToolUseFailure
lookup reads SQLite only:
- an exact signature match first, then a lexical-overlap fallback;
- no model load and no Qdrant lock;
- it emits nothing if the latency budget (700 ms by default) is exceeded;
- it dedupes per (session, signature) and is rate-limited per session;
- it only ever returns verified lessons from the same workspace that have a recorded fix;
- the text is bounded to 1,500 characters, sanitized, and labelled as untrusted and non-authoritative.

**Embedded vs server Qdrant.** Embedded local mode is the default because it needs no
Docker. It takes a file lock (one process at a time; afr waits up to 5 s, then fails with a
clear message). It also ignores payload indexes, so afr only creates them on a server. Its
performance isn't representative. With `AFR_QDRANT_URL`, the same code uses a server. Both
modes are tested; see `tests/test_qdrant_server.py` and the `qdrant-server` CI job. The
benchmark produced identical quality metrics in both modes ([embedded](benchmark.md),
[server](benchmark-server.md)).

## Acceptance checklist

| requirement | status | where |
|---|---|---|
| typed records for all required entities | done | `domain/models.py` |
| SQLite/WAL + Alembic migrations + schema version | done | `persistence/`, `afr doctor` |
| Claude Code hook receiver (documented fields only) | done, tested against doc payloads | `adapters/claude_code/`, `tests/fixtures/claude_code/` |
| hook install: project scope, diff, preserve, uninstall, no global edits | done | `installer.py`, `tests/test_installer.py` |
| generic JSONL importer + schema + fixtures | done | `schemas/generic-event-v1.schema.json`, `examples/events.jsonl` |
| Codex importer | **not implemented** (schema not verified; see backlog) | — |
| capture levels; redaction before persistence/indexing/retrieval/export | done | `redaction/`, boundary tests |
| deletion, export, retention | done | `afr episode delete`, `afr export`, `afr purge` |
| fail-open receiver, counters, spool, replay, concurrency/recovery tests | done | `receiver.py`, `ingest/spool.py`, `tests/test_receiver.py` |
| failure signatures, outcome states, objective verification | done | `episodes/signatures.py`, `verification.py` |
| task vs root-cause verification separated | done | `tests/test_verification.py` |
| evidence-bounded lessons, provenance, supersession/invalidation | done | `lessons/` |
| real BM25 + dense + RRF hybrid in Qdrant, filters, modes | done | `retrieval/` |
| mandatory workspace isolation | done | `search.build_filter`, tests |
| applicability policy with cautions | done | `retrieval/policy.py` |
| MCP server: search / evidence / outcome feedback | done | `mcp_server/server.py`, `tests/test_mcp.py` |
| proactive PostToolUseFailure context (opt-in, bounded) | done | `proactive.py`, `tests/test_proactive.py` |
| experience-bundle.v1 schema, validation, round trip | done | `schemas/`, `export/bundle.py`, `tests/test_export.py` |
| offline benchmark: 6 case types, qrels, Recall/MRR/nDCG, latency, splits, leakage guard | done | `benchmarks/`, `eval/harness.py` |
| optional live paired runner | implemented; **never run** (no live results exist) | `eval/live.py` |
| demos: repeated failure → verified fix → retrieval; inapplicable fix flagged | done | `afr demo` |
| CI: lint, types, tests, server-mode job | done | `.github/workflows/ci.yml` |
