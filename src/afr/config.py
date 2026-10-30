"""Runtime settings.

Settings come from environment variables (prefix ``AFR_``) with conservative defaults.
Nothing here reads or writes user-global agent configuration.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class CaptureLevel(StrEnum):
    """What the recorder is allowed to persist for new events.

    ``metadata``: allowlisted structured fields only (tool name, category, exit status,
    normalized + redacted failure signature, timing). No free text excerpts.
    ``sanitized_evidence``: additionally keeps redacted, size-bounded excerpts of error
    output and command lines, which is what automatic lesson extraction needs.
    """

    METADATA = "metadata"
    SANITIZED_EVIDENCE = "sanitized_evidence"


def _default_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agent-flight-recorder"


class Settings(BaseModel):
    home: Path = Field(default_factory=_default_home)
    workspace: str = "default"
    capture_level: CaptureLevel = CaptureLevel.METADATA
    qdrant_url: str | None = None
    qdrant_timeout_s: int = 10
    index_prefix: str = "afr"
    embedding_provider: str = "sentence-transformers"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    max_excerpt_chars: int = 1200
    max_event_bytes: int = 64_000
    retention_days: int | None = None
    stale_after_days: int = 180
    spool_max_bytes: int = 5_000_000
    proactive_budget_ms: int = 700
    proactive_max_per_session: int = 3

    @property
    def db_path(self) -> Path:
        return self.home / "afr.sqlite3"

    @property
    def qdrant_path(self) -> Path:
        return self.home / "qdrant"

    @property
    def spool_path(self) -> Path:
        return self.home / "spool.jsonl"

    @property
    def salt_path(self) -> Path:
        return self.home / "salt"

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        env = os.environ
        data: dict[str, object] = {}
        mapping = {
            "AFR_HOME": "home",
            "AFR_WORKSPACE": "workspace",
            "AFR_CAPTURE_LEVEL": "capture_level",
            "AFR_QDRANT_URL": "qdrant_url",
            "AFR_INDEX_PREFIX": "index_prefix",
            "AFR_EMBEDDING_PROVIDER": "embedding_provider",
            "AFR_EMBEDDING_MODEL": "embedding_model",
            "AFR_RETENTION_DAYS": "retention_days",
            "AFR_STALE_AFTER_DAYS": "stale_after_days",
        }
        for var, field in mapping.items():
            if env.get(var):
                data[field] = env[var]
        data.update({k: v for k, v in overrides.items() if v is not None})
        settings = cls.model_validate(data)
        settings.home = settings.home.expanduser()
        return settings
