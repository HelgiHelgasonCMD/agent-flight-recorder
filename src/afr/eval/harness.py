"""Offline retrieval benchmark.

Loads the synthetic corpus into a fresh, isolated store + index, then runs every query
in each mode. Leakage controls:
- time split: each query only sees records created before its ``as_of`` (Qdrant range
  filter), and any returned record at/after ``as_of`` fails the run;
- family split: queries are labelled dev/test by task family and reported separately.
Results describe *retrieval quality on synthetic fixtures*. They are not evidence that
agents perform better; that needs paired live runs (see ``afr eval live``).
"""

from __future__ import annotations

import json
import platform
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from afr import __version__
from afr.adapters.generic_jsonl import import_file
from afr.app import App
from afr.config import CaptureLevel, Settings
from afr.eval.metrics import mrr, ndcg_at_k, percentile, recall_at_k
from afr.retrieval.policy import QueryContext
from afr.retrieval.search import SearchRequest

DEFAULT_CORPUS = Path(__file__).resolve().parents[3] / "benchmarks" / "corpus"
K_VALUES = (1, 3, 5)


class LeakageError(AssertionError):
    pass


@dataclass
class QueryResult:
    qid: str
    mode: str
    policy: bool
    ranking: list[str]
    latency_ms: float
    metrics: dict[str, float]
    trap_flags: dict[str, bool] = field(default_factory=dict)
    trap_below_correct: bool | None = None


def _ranking(hits: list[Any]) -> list[str]:
    out: list[str] = []
    for h in hits:
        eid = h.episode_ids[0] if h.episode_ids else h.doc_id
        if eid not in out:
            out.append(eid)
    return out


def evaluate_query(
    app: App, q: dict[str, Any], mode: str, policy: bool, episodes: dict[str, Any], k: int = 10
) -> QueryResult:
    as_of = datetime.fromisoformat(q["as_of"])
    ctx = q.get("context", {})
    req = SearchRequest(
        query=q["query"],
        workspace=app.settings.workspace,
        mode=mode,  # type: ignore[arg-type]
        k=k,
        until=as_of,
        apply_policy=policy,
        context=QueryContext(repo_id=ctx.get("repo_id"), os=ctx.get("os"), versions=ctx.get("versions", {}), now=as_of),
    )
    with app.searcher(sync=False) as s:
        t0 = time.perf_counter()
        res = s.search(req)
        latency = (time.perf_counter() - t0) * 1000
    ranking = _ranking(res.hits)
    for eid in ranking:
        rec_ts = datetime.fromisoformat(episodes[eid]["ts"]) if eid in episodes else None
        if rec_ts is not None and rec_ts >= as_of:
            raise LeakageError(f"{q['id']}: future record {eid} ({rec_ts}) retrieved for as_of {as_of}")
    qrels = q["qrels"]
    metrics = {f"recall@{kk}": recall_at_k(ranking, qrels, kk) for kk in K_VALUES}
    metrics["mrr@10"] = mrr(ranking, qrels, 10)
    metrics["ndcg@5"] = ndcg_at_k(ranking, qrels, 5)
    flags: dict[str, bool] = {}
    for trap, codes in q.get("traps", {}).items():
        hit = next((h for h in res.hits if h.episode_ids and h.episode_ids[0] == trap), None)
        if hit is not None:
            flags[trap] = any(c["code"] in codes for c in hit.cautions)
    below: bool | None = None
    correct = [d for d, g in qrels.items() if g >= 2 and d not in q.get("traps", {})]
    traps_in = [t for t in q.get("traps", {}) if t in ranking]
    if correct and traps_in:
        best = min((ranking.index(c) for c in correct if c in ranking), default=None)
        below = best is not None and all(ranking.index(t) > best for t in traps_in)
    return QueryResult(q["id"], mode, policy, ranking, latency, metrics, flags, below)


def load_corpus(app: App, corpus: Path) -> dict[str, Any]:
    meta: dict[str, Any] = json.loads((corpus / "queries.json").read_text())
    rep = import_file(app.pipeline, corpus / "history.jsonl", CaptureLevel.SANITIZED_EVIDENCE, app.settings.workspace)
    if rep.invalid:
        raise ValueError(f"invalid corpus records: {rep.invalid[:3]}")
    return meta


def summarize(results: list[QueryResult], queries: list[dict[str, Any]]) -> dict[str, Any]:
    by_q = {q["id"]: q for q in queries}
    groups: dict[tuple[str, bool], list[QueryResult]] = defaultdict(list)
    for r in results:
        groups[(r.mode, r.policy)].append(r)
    out: dict[str, Any] = {}
    for (mode, policy), rs in groups.items():
        name = f"{mode}{'' if policy else ' (no policy)'}"
        entry: dict[str, Any] = {"queries": len(rs)}
        for split in ("all", "dev", "test"):
            sel = [r for r in rs if split == "all" or by_q[r.qid]["split"] == split]
            if not sel:
                continue
            entry[split] = {m: round(sum(r.metrics[m] for r in sel) / len(sel), 3) for m in sel[0].metrics}
            entry[split]["n"] = len(sel)
        cases: dict[str, list[QueryResult]] = defaultdict(list)
        for r in rs:
            cases[by_q[r.qid]["case"]].append(r)
        entry["by_case"] = {
            c: {
                "n": len(v),
                "mrr@10": round(sum(r.metrics["mrr@10"] for r in v) / len(v), 3),
                "recall@3": round(sum(r.metrics["recall@3"] for r in v) / len(v), 3),
            }
            for c, v in sorted(cases.items())
        }
        flags = [f for r in rs for f in r.trap_flags.values()]
        below = [r.trap_below_correct for r in rs if r.trap_below_correct is not None]
        entry["traps"] = {
            "flagged": f"{sum(flags)}/{len(flags)}",
            "ranked_below_correct": f"{sum(below)}/{len(below)}",
        }
        lat = [r.latency_ms for r in rs]
        entry["latency_ms"] = {"p50": round(percentile(lat, 50), 1), "p95": round(percentile(lat, 95), 1)}
        out[name] = entry
    return out


def render_markdown(summary: dict[str, Any], env: dict[str, Any]) -> str:
    lines = [
        "# Synthetic retrieval benchmark",
        "",
        "> Retrieval quality on the synthetic fixture corpus in `benchmarks/corpus/` "
        "(generated by `benchmarks/build_corpus.py`). This is **not** a measurement of agent task success; "
        "no live-agent results are reported here.",
        "",
        f"- generated: {env['generated_at']}  ·  afr {env['afr_version']}  ·  embedding: `{env['embedding']}` "
        f"(semantic={env['semantic']})  ·  qdrant: {env['qdrant']}",
        f"- corpus: {env['episodes']} episodes, {env['queries']} queries (dev {env['dev']}, test {env['test']}); "
        "time-split leakage guard passed",
        f"- machine: {env['machine']}",
        "",
        "## Overall (all queries)",
        "",
        "| mode | Recall@1 | Recall@3 | Recall@5 | MRR@10 | nDCG@5 | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, e in summary.items():
        a = e["all"]
        lines.append(
            f"| {name} | {a['recall@1']:.3f} | {a['recall@3']:.3f} | {a['recall@5']:.3f} | {a['mrr@10']:.3f} | "
            f"{a['ndcg@5']:.3f} | {e['latency_ms']['p50']} | {e['latency_ms']['p95']} |"
        )
    lines += ["", "## By split (MRR@10 / Recall@3)", "", "| mode | dev | test |", "|---|---|---|"]
    for name, e in summary.items():
        d, t = e.get("dev", {}), e.get("test", {})
        lines.append(
            f"| {name} | {d.get('mrr@10', 0):.3f} / {d.get('recall@3', 0):.3f} (n={d.get('n', 0)}) | "
            f"{t.get('mrr@10', 0):.3f} / {t.get('recall@3', 0):.3f} (n={t.get('n', 0)}) |"
        )
    cases = sorted({c for e in summary.values() for c in e["by_case"]})
    lines += [
        "",
        "## By case type (MRR@10)",
        "",
        "| mode | " + " | ".join(cases) + " |",
        "|---" * (len(cases) + 1) + "|",
    ]
    for name, e in summary.items():
        lines.append(
            f"| {name} | "
            + " | ".join(f"{e['by_case'][c]['mrr@10']:.3f} (n={e['by_case'][c]['n']})" for c in cases)
            + " |"
        )
    lines += [
        "",
        "## Trap handling",
        "",
        "Traps are records that look relevant but are inapplicable (wrong OS precondition, major-version mismatch, "
        "stale, failed or merely claimed fixes). *flagged* = trap appeared in the top 10 with an expected caution; "
        "*ranked below correct* = every trap ranked below the best applicable answer.",
        "",
        "| mode | flagged | ranked below correct |",
        "|---|---|---|",
    ]
    for name, e in summary.items():
        lines.append(f"| {name} | {e['traps']['flagged']} | {e['traps']['ranked_below_correct']} |")
    lines.append("")
    return "\n".join(lines)


def run(
    modes: list[str],
    embedding: str | None = None,
    corpus: Path | None = None,
    qdrant_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[QueryResult]]:
    corpus = corpus or DEFAULT_CORPUS
    base = Settings.from_env()
    with tempfile.TemporaryDirectory(prefix="afr-eval-") as tmp:
        settings = Settings(
            home=Path(tmp),
            workspace="bench",
            embedding_provider=embedding or base.embedding_provider,
            embedding_model=base.embedding_model,
            qdrant_url=qdrant_url,
        )
        app = App(settings)
        meta = load_corpus(app, corpus)
        if qdrant_url:  # isolate from any previous benchmark collection on a shared server
            app.sync_index(rebuild=True)
        else:
            app.sync_index()
        queries = meta["queries"]
        results: list[QueryResult] = []
        for mode in modes:
            # warm-up so model loading is not counted as query latency
            evaluate_query(app, queries[0], mode, True, meta["episodes"])
            for q in queries:
                results.append(evaluate_query(app, q, mode, True, meta["episodes"]))
        if "hybrid" in modes:
            for q in queries:
                results.append(evaluate_query(app, q, "hybrid", False, meta["episodes"]))
        summary = summarize(results, queries)
        env = {
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "afr_version": __version__,
            "embedding": app.provider.name
            if app.provider.semantic
            else f"{app.provider.name} (NON-SEMANTIC test stand-in)",
            "semantic": app.provider.semantic,
            "qdrant": f"server {qdrant_url}" if qdrant_url else "embedded (local mode)",
            "episodes": len(meta["episodes"]),
            "queries": len(queries),
            "dev": sum(1 for q in queries if q["split"] == "dev"),
            "test": sum(1 for q in queries if q["split"] == "test"),
            "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        }
        return summary, env, results


def run_benchmark(
    modes: list[str], output: Path | None, json_out: Path | None, embedding: str | None, corpus: Path | None
) -> int:
    import typer

    settings = Settings.from_env()
    summary, env, results = run(modes, embedding, corpus, settings.qdrant_url)
    md = render_markdown(summary, env)
    typer.echo(md)
    if output:
        output.write_text(md)
    if json_out:
        json_out.write_text(
            json.dumps({"env": env, "summary": summary, "per_query": [r.__dict__ for r in results]}, indent=2)
        )
    return 0
