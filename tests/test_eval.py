from __future__ import annotations

import json
from pathlib import Path

import pytest

from afr.eval import harness
from afr.eval.metrics import mrr, ndcg_at_k, percentile, recall_at_k

ROOT = Path(__file__).resolve().parents[1]


def test_metrics() -> None:
    q = {"a": 2, "b": 1}
    assert recall_at_k(["x", "a", "b"], q, 2) == 0.5
    assert mrr(["x", "a"], q) == 0.5
    assert mrr(["b", "x"], q) == 0.0  # only grade-2 counts as the answer
    assert ndcg_at_k(["a", "b"], q, 5) == pytest.approx(1.0)
    assert ndcg_at_k(["b", "a"], q, 5) < 1.0
    assert percentile([1, 2, 3, 4], 50) == 2


def test_corpus_is_deterministic(tmp_path: Path) -> None:
    import subprocess
    import sys

    before = (ROOT / "benchmarks/corpus/history.jsonl").read_bytes()
    subprocess.run([sys.executable, str(ROOT / "benchmarks/build_corpus.py")], check=True, capture_output=True)
    assert (ROOT / "benchmarks/corpus/history.jsonl").read_bytes() == before


def test_corpus_covers_required_cases() -> None:
    meta = json.loads((ROOT / "benchmarks/corpus/queries.json").read_text())
    cases = {q["case"] for q in meta["queries"]}
    assert {
        "exact_repeat",
        "paraphrase",
        "stale_fix",
        "misleading_prior_failure",
        "version_mismatch",
        "different_root_cause",
    } <= cases
    assert {q["split"] for q in meta["queries"]} == {"dev", "test"}
    for q in meta["queries"]:  # judgments never point at records from the query's future
        for eid in q["qrels"]:
            assert meta["episodes"][eid]["ts"] < q["as_of"], (q["id"], eid)


def test_benchmark_runs_and_guards_leakage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AFR_INDEX_PREFIX", "evaltest")
    summary, env, results = harness.run(["bm25", "hybrid"], embedding="hashing")
    assert env["semantic"] is False and "NON-SEMANTIC" in env["embedding"]
    assert summary["bm25"]["all"]["n"] == env["queries"]
    q26 = next(r for r in results if r.qid == "q26" and r.mode == "hybrid")
    assert "f15_future" not in q26.ranking  # answer recorded after as_of is invisible


def test_leakage_guard_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from afr.retrieval.search import Searcher

    original = Searcher.search

    def leaky(self, req):  # drop the time filter to simulate a leak
        req.until = None
        return original(self, req)

    monkeypatch.setattr(Searcher, "search", leaky)
    with pytest.raises(harness.LeakageError):
        harness.run(["bm25"], embedding="hashing")


def test_live_runner_refuses_without_opt_in(tmp_path: Path) -> None:
    from afr.eval.live import run_live

    assert run_live(ROOT / "benchmarks/live/tasks.json", "claude", False, False, tmp_path / "o.json") == 2


def test_live_runner_plumbing_with_fake_agent(tmp_path: Path) -> None:
    """Plumbing only: a fake agent script stands in for the CLI. Not evidence of anything."""
    from afr.eval.live import run_live

    fake = tmp_path / "fake-agent"
    fake.write_text("#!/bin/sh\nsed -i 's/2,/2/' config.json\necho '{\"num_turns\": 1, \"is_error\": false}'\n")
    fake.chmod(0o755)
    out = tmp_path / "live.json"
    assert run_live(ROOT / "benchmarks/live/tasks.json", str(fake), True, False, out) == 0
    data = json.loads(out.read_text())
    assert data["kind"] == "live-agent-paired"
    assert {r["arm"] for r in data["runs"]} == {"control", "retrieval"}
    assert all(r["task_success"] and r["num_turns"] == 1 for r in data["runs"])
    assert "total_cost_usd" not in data["runs"][0]  # not reported → not invented
