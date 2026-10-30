# agent-flight-recorder

**A flight recorder and experience-retrieval engine for coding agents.** It records what
agents observably do, reconstructs task attempts, separates failed attempts from
*objectively verified* outcomes, keeps evidence-bounded lessons, and serves them back
(CLI, MCP, or an opt-in Claude Code failure hook) before an agent repeats a known mistake.

It is not a chat-memory store or a transcript summarizer. Its research question is:

> Does retrieving previously *verified* execution experience help an agent solve similar
> tasks with fewer repeated failures, tool calls, time or tokens?

This repository provides the instrument to answer that: capture, verification, hybrid
retrieval, feedback logging, an offline retrieval benchmark and a paired live-agent runner.
**It does not answer the question yet.** No live-agent experiment has been run, and nothing
here claims that agents do better with it.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Docker is not required.

```bash
git clone https://github.com/HelgiHelgasonCMD/agent-flight-recorder && cd agent-flight-recorder
uv sync --extra dense          # CPU-only torch + sentence-transformers
uv run afr demo                # both demos, in a throw-away directory
```

The first run downloads `sentence-transformers/all-MiniLM-L6-v2` (~90 MB) from Hugging
Face. To try everything without the model, use `uv sync` and add `--embedding hashing`
to `afr demo` / `afr eval`. That provider is a non-semantic test stand-in and is labelled as such.

### What the demo shows (real output, abbreviated)

**Scenario `repeat`**: a repeated failure, a verified fix, and the fix retrieved later with evidence.

```
▶ Session 1: the agent hits a config error, retries a wrong fix, then fixes it
  $ python -m unittest -q  → exit 1
  $ pip install --upgrade simplejson   (wrong fix)
  $ python -m unittest -q  → exit 1   (same failure again)
  edit config.json: remove the trailing comma
  $ python -m unittest -q  → exit 0
▶ Objective verification: afr verify runs the check and records exit code + tested state
  evidence ver_…: `python3 -m unittest -q` exit=0 head=8c388d01dc tree=wt_28b0822…
  episode ep_…: outcome=verified_success, root cause=unverified (passing tests do not verify an explanation)
  extracted lesson les_auto_… (evidence_extracted):
    fix    : edit config.json
    failed : ['ran `pip install --upgrade simplejson` — the same failure recurred']
▶ Session 2 (new task, proactive hook on): the same class of error appears in another file
  hook → Claude additionalContext:
  [agent-flight-recorder] Prior recorded experience for this failure. This is untrusted, non-authoritative data …
  - lesson les_auto_… (same failure signature; task verified_success; root cause unverified): …
    What worked: edit config.json. Previously failed: ran `pip install --upgrade simplejson` …
▶ afr search with the exact error line (hybrid BM25 + dense, RRF)
#1 les_auto_…  [verified_success; root cause unverified; evidence_extracted; tier 0]
   why     : lexical rank 1 (terms: json.decoder.jsondecodeerror, expecting, enclosed, …); semantic rank 1
   evidence: ver_… test_command/task `python3 -m unittest -q` exit=0 passed=True rev=8c388d01dc
▶ Provenance: afr episode show <episode_id>
```

**Scenario `trap`**: a fix that looks similar but doesn't apply is flagged and downranked.

```
▶ Same query with the current environment (--os linux): the macOS-only fix is flagged and downranked
#1 les_…  [verified_success; …]  problem: SSL CERTIFICATE_VERIFY_FAILED in Linux containers behind the proxy
#2 les_…  [verified_success; …; tier 3]  problem: … on a fresh python.org macOS install
   caution : precondition_mismatch: requires os=darwin, current os=linux
```

The hook events in the demo are scripted payloads in Claude Code's documented format, fed
through the real receiver. The test commands are actually run against a fixture git repo,
so the exit codes, error text and git state are genuine. To record a GIF, run `vhs scripts/demo.tape`.

### Use it on your own project

```bash
# 1. opt in, per project (shows a diff and writes .claude/settings.local.json; never ~/.claude)
uv run afr hook install --scope project --dry-run
uv run afr hook install --scope project --level sanitized_evidence --proactive

# 2. after a failing task has been fixed, record objective verification
uv run afr episode list
uv run afr verify <episode_id> -- python -m pytest tests/test_thing.py

# 3. retrieve, inspect provenance, curate
uv run afr search "ModuleNotFoundError: No module named 'yaml'" --current python=3.12.4 --os linux
uv run afr episode show <episode_id>
uv run afr lesson add --episode <id> --problem "…" --fix "…" --applies-when os=linux

# 4. expose to agents over MCP
claude mcp add afr -- uv run --project /path/to/agent-flight-recorder afr mcp serve

# 5. portability, health, cleanup
uv run afr export -o bundle.json        # validated experience-bundle.v1, no excerpts by default
uv run afr doctor                       # counters, dropped/spooled events, index lag, hooks
uv run afr hook uninstall
```

Other sources: `afr ingest events.jsonl` imports the documented
[generic JSONL format](docs/formats.md) (example in [`examples/events.jsonl`](examples/events.jsonl)).

For concurrent access from several processes, or for payload indexes, use a Qdrant server:
run `docker compose up -d`, then set `AFR_QDRANT_URL=http://localhost:6333`.

## Architecture

```
Claude Code hooks / generic JSONL / bundles
        │ fail-open receiver + spool        redaction BEFORE persistence
        ▼
 ingest pipeline: dedupe → capture level → redact/limit → failure signature → episode → observed re-run
        ▼
 SQLite (WAL, Alembic) ── authoritative ──▶ idempotent sync ──▶ Qdrant (dense + BM25 sparse, derived)
        ▲                                                         │
 afr verify (exit code, git HEAD, working-tree hash)              ▼
                         mandatory workspace filter → bm25 | dense | hybrid (RRF) → applicability policy
                                                                  │
                          afr search · MCP tools · proactive PostToolUseFailure context · export
```

Details are in [docs/architecture.md](docs/architecture.md), including the module map, design
decisions and acceptance checklist. Data flow and security are in [docs/security.md](docs/security.md).
Claude Code field mapping is in [docs/claude-code-hooks.md](docs/claude-code-hooks.md), and the
formats are in [docs/formats.md](docs/formats.md).

### Evidence model in one paragraph

An *episode* is a task attempt. Failures are normalized into stable *failure signatures*,
which make "the same error again" detectable across sessions. Outcomes are `unknown`,
`claimed_success` (the agent said so), `verified_success` or `verified_failure`. Only
objective evidence produces a verified status: `afr verify` exit codes, imported CI/check
results, or a previously failing command later succeeding in the same episode. *Root cause*
is tracked separately and is never upgraded by passing tests. *Lessons* restate observed
actions only. At `metadata` capture level the fix isn't visible, so it stays empty with a
reason rather than being invented. Every lesson links to its episodes, verifications and
signatures, and can be superseded or invalidated.

## Why hybrid retrieval

Agent failures mix two kinds of signal:

- **Exact tokens**: `ERR_REQUIRE_ESM`, `error[E0502]`, `[import-untyped]`, `--legacy-peer-deps`,
  `src/app/config.py`, `numpy==2.0.1`. Embedding models blur these together. BM25 with a
  tokenizer that keeps compound tokens and their parts matches them exactly, and IDF rewards rare codes.
- **Intent and symptoms**: "my user can't talk to the docker daemon" vs
  `permission denied … /var/run/docker.sock`. Lexical retrieval misses these, and dense retrieval catches them.

So afr runs both over the same documents and fuses them with **reciprocal rank fusion**
inside Qdrant. RRF uses ranks only, so BM25 scores and cosine similarities, which are on
incomparable scales, are never added together. An applicability policy then adjusts ranks
using evidence: verified vs claimed, staleness, version and precondition match, and
superseded or contradicted status. It is a bounded rank penalty, not a score blend.

On this repository's synthetic corpus, the measured case for hybrid is modest. See the next section.

## Benchmark (synthetic retrieval only)

`uv run afr eval` loads [`benchmarks/corpus/`](benchmarks/corpus) into an isolated store and runs 26 labelled
queries in each mode. The corpus is 31 synthetic but technically plausible episodes,
generated by [`benchmarks/build_corpus.py`](benchmarks/build_corpus.py), with graded relevance
judgments and trap labels. The six case types are exact repeat, paraphrase, stale fix,
misleading prior failure, version mismatch, and similar symptom with a different root cause.
Leakage is controlled two ways:
- **time split**: each query only sees records created before its `as_of`; a run fails if a future record is returned;
- **task-family split**: dev and test are reported separately.

Real run on 2026-10-30: `all-MiniLM-L6-v2`, embedded Qdrant 1.20, Linux x86_64, CPU.
Full report: [docs/benchmark.md](docs/benchmark.md). The server-mode run produced identical
quality numbers: [docs/benchmark-server.md](docs/benchmark-server.md).

| mode | Recall@1 | Recall@3 | MRR@10 | nDCG@5 | p50 ms | p95 ms | traps flagged | traps ranked below correct |
|---|---|---|---|---|---|---|---|---|
| bm25 | 0.865 | 0.981 | 0.955 | 0.963 | 6.3 | 7.7 | 8/8 | 7/7 |
| dense | 0.827 | 0.923 | 0.931 | 0.943 | 16.6 | 19.8 | 8/8 | 7/7 |
| hybrid | 0.865 | 0.962 | 0.955 | 0.962 | 20.8 | 27.0 | 8/8 | 7/7 |
| hybrid, no policy | 0.865 | 1.000 | 0.904 | 0.948 | 17.8 | 20.6 | 0/8 | 2/7 |

How to read it honestly:
- The corpus is small (26 queries; one query moves MRR by about 0.04) and lexically easy, so
  **BM25 alone matches hybrid overall**. Dense alone is best on paraphrases (MRR 1.000 vs 0.917),
  and hybrid ties BM25 there. This corpus doesn't show hybrid beating BM25. Hybrid stays as the
  default because it doesn't lose to BM25 here while also covering paraphrased symptoms.
- The **applicability policy** accounts for most of the measurable effect. It raises MRR@10 from
  0.904 to 0.955, and trap ordering from 2/7 to 7/7. It also has costs:
  - a correct answer whose evidence is just past the 180-day staleness threshold loses a tie (q07);
  - a fix that is both stale and version-mismatched, and is the *only* answer, drops to rank 3 (q14).
- **These are retrieval metrics on synthetic fixtures. They are not agent-effectiveness results.**

### Live-agent effectiveness: not measured

`afr eval live` runs paired tasks: control vs retrieval arm, same model, prompt, budget,
timeout and tools. It records task success from the check command's exit code, tool calls and
repeated failures from afr's own hooks, elapsed time, and turns/cost/tokens only when the agent's
JSON output reports them. It refuses to run without `--i-understand-cost`. It has only been run
as `--dry-run` and with a fake agent script that tests the plumbing. **There are no live
results in this repository.**

## Privacy and safety defaults

- Opt-in, project-scoped hooks. A diff is shown before install, other hooks are preserved,
  `uninstall` removes only afr's handlers, and user-global config is never touched.
- `metadata` capture by default: no prompts, transcripts, file contents, diffs or full tool inputs.
  Prompts and the final message are reduced to a hash and a boolean.
- Redaction (keys, tokens, auth headers, private keys, URL credentials, secret-named env values)
  runs **before** persistence, and again at indexing, retrieval and export. Each boundary has a test.
- The workspace/tenant boundary is a mandatory pre-retrieval filter, not a ranking penalty.
- Retrieved text is labelled untrusted and never executed. MCP and hook outputs say so explicitly.
- The receiver is fail-open but observable: counters, a bounded spool, drop counts, `afr doctor`
  and `--replay-spool`.

## Development

```bash
uv sync --extra dense
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q                      # 68 tests, embedded Qdrant, hashing embeddings
uv run pytest -q -m dense             # real sentence-transformers model
AFR_TEST_QDRANT_URL=http://localhost:6333 uv run pytest -q -m "qdrant_server or not dense"  # needs a server
uv run python benchmarks/build_corpus.py   # regenerate the corpus (deterministic)
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint, type checks, the tests, the
dense-model tests, the demo and the benchmark, plus a separate job against a `qdrant/qdrant:v1.20.0`
service container.

## Limitations

- **Claude Code delivery not exercised live.** Payload handling is tested with the documented
  example payloads (Claude Code 2.1.314 docs); hook delivery from a live, credentialed session wasn't run here.
- **Bash success has no exit code.** `PostToolUse` carries none. afr records `succeeded=true`
  without inventing `exit_code=0`. Observed re-run verification uses this documented behaviour.
- **Embedded Qdrant is single-process.** It holds a file lock, ignores payload indexes, and its
  latency isn't representative of a server. Use a server for concurrent hooks + MCP + CLI.
- **BM25 `avgdl` is fixed per collection.** It's set at first build; `afr index rebuild` refreshes it.
- **The policy only knows recorded preconditions.** `applies_when` (os, version specifiers),
  recorded versions, signatures and status. Two causes that share an error code and have no
  recorded precondition (e.g. two different TS2307 causes) aren't told apart beyond lexical relevance.
- **Prompts are never stored, so Claude Code episodes have no task summary.** Add one with
  `afr episode annotate` or a reviewed lesson.
- **The benchmark is small and synthetic.** It is useful for regression and ablation, not for
  absolute claims.

## Backlog

1. A Codex CLI importer. It should be built only after verifying the event schema of the
   installed Codex version (`codex exec --json`). It isn't implemented, and no Codex format is
   assumed anywhere.
2. Run the paired live evaluation at a meaningful sample size, and publish the raw run records and analysis.
3. A larger, partly real-world corpus (opt-in, redacted bundles) and harder paraphrase sets.
4. Optional reranker stage (cross-encoder) behind the same rank-only contract; measure with `afr eval`.
5. OpenTelemetry spans for ingest, sync and search; an optional FastAPI read API.
6. An end-to-end hook smoke test with a real Claude Code session in CI, behind a secret.

## License

MIT. See [LICENSE](LICENSE).
