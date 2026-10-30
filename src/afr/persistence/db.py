"""SQLite engine setup (WAL, busy timeout) and migration runner."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError

from afr.persistence.tables import SCHEMA_HEAD

BUSY_TIMEOUT_MS = 3000


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": BUSY_TIMEOUT_MS / 1000},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        try:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        except OperationalError:
            return None
    return row[0] if row else None


def migrate(engine: Engine) -> None:
    """Upgrade to head. Cheap no-op when already current (hot path for hooks)."""
    if current_revision(engine) == SCHEMA_HEAD:
        return
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def with_retry[T](fn: Callable[[], T], attempts: int = 4, base_delay: float = 0.05) -> T:
    """Bounded retry for SQLite lock contention. Re-raises after the last attempt."""
    for i in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2**i))
    raise AssertionError("unreachable")
