"""Health checks. Makes silent data loss visible."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afr.app import App
from afr.ingest.spool import spool_stats
from afr.persistence.db import current_revision
from afr.persistence.tables import SCHEMA_HEAD


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def run(app: App, check_index: bool = True, project: Path | None = None) -> list[Check]:
    s = app.settings
    out: list[Check] = []
    rev = current_revision(app.store.engine)
    out.append(Check("database", rev == SCHEMA_HEAD, f"{s.db_path} schema={rev} (head {SCHEMA_HEAD})"))

    counts = app.store.counts()
    out.append(Check("records", True, ", ".join(f"{k}={v}" for k, v in counts.items()), counts))

    stats = app.store.stats()
    failed = stats.get("failed", 0)
    out.append(
        Check(
            "ingest_counters",
            failed == 0,
            ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "no events received yet",
            stats,
        )
    )
    sp = spool_stats(s.spool_path)
    dropped = int(sp.get("dropped", 0))
    pending = int(sp.get("pending", 0))
    detail = f"pending={pending} spooled_total={sp.get('spooled', 0)} dropped={dropped}"
    if sp.get("last_error"):
        detail += f" last_error={str(sp['last_error']).splitlines()[0][:160]}"
    if pending:
        detail += " → run `afr ingest --replay-spool`"
    out.append(Check("spool", dropped == 0 and pending == 0, detail, sp))

    dense_ok = importlib.util.find_spec("sentence_transformers") is not None
    out.append(
        Check(
            "embeddings",
            dense_ok or s.embedding_provider == "hashing",
            f"provider={s.embedding_provider} model={s.embedding_model} sentence-transformers installed={dense_ok}",
        )
    )

    if check_index:
        try:
            from afr.retrieval.index import Index, open_client

            with open_client(s) as client:
                idx = Index(app.store, client, app.provider)
                pending_docs = idx.pending()
                info = idx.info()
            mode = f"server {s.qdrant_url}" if s.qdrant_url else f"embedded {s.qdrant_path}"
            out.append(
                Check(
                    "index",
                    pending_docs == 0,
                    f"{mode} collection={info['collection']} points={info['points']} pending={pending_docs}"
                    + (" → run `afr index sync`" if pending_docs else ""),
                    info,
                )
            )
        except Exception as exc:
            out.append(Check("index", False, f"{type(exc).__name__}: {exc}"))

    if project is not None:
        for name in ("settings.local.json", "settings.json"):
            p = project / ".claude" / name
            if p.exists():
                try:
                    data = json.loads(p.read_text())
                    from afr.adapters.claude_code.installer import is_afr_handler

                    n = sum(
                        1
                        for groups in (data.get("hooks") or {}).values()
                        for g in groups
                        for h in g.get("hooks", [])
                        if is_afr_handler(h)
                    )
                    out.append(Check(f"hooks:{name}", True, f"{n} afr hook handler(s) in {p}"))
                except (OSError, ValueError, AttributeError) as exc:
                    out.append(Check(f"hooks:{name}", False, f"cannot parse {p}: {exc}"))
    return out
