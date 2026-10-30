"""Locate the published JSON Schemas (repo ``schemas/`` dir, packaged as ``afr/_schemas``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PACKAGED = Path(__file__).parent / "_schemas"
_REPO = Path(__file__).resolve().parents[2] / "schemas"


def schema_path(name: str) -> Path:
    for base in (_PACKAGED, _REPO):
        p = base / name
        if p.exists():
            return p
    raise FileNotFoundError(name)


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(schema_path(name).read_text())
    return data
