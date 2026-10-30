from __future__ import annotations

import copy
import json

import pytest

from afr.app import App
from afr.config import Settings
from afr.export.bundle import BundleValidationError, export_bundle, import_bundle, validate
from afr.retrieval.embeddings import HashingProvider

from .test_retrieval import SECRET, episode, load


def _strip_volatile(b: dict) -> dict:
    b = copy.deepcopy(b)
    for k in ("export_id", "created_at"):
        b.pop(k)
    return b


def test_export_validates_and_omits_excerpts_by_default(app: App) -> None:
    load(app, episode("x1", 0, f"PermissionError: denied {SECRET}", "chmod +x scripts/run.sh"))
    bundle = export_bundle(app.store, app.settings.workspace)
    validate(bundle)
    assert bundle["schema_version"] == "experience-bundle.v1"
    assert bundle["redaction"] == {"applied": True, "rules_version": "afr-redaction.v1", "excerpts_included": False}
    ep = bundle["episodes"][0]
    assert ep["outcome"]["status"] == "verified_success"
    assert all(a["command"] is None for a in ep["attempts"])
    assert all(o["excerpt"] is None for o in ep["observations"])
    assert ep["lessons"] and ep["lessons"][0]["verification"]["task_status"] == "verified_success"
    assert SECRET not in json.dumps(bundle)
    with_ex = export_bundle(app.store, app.settings.workspace, include_excerpts=True)
    assert any(a["command"] for a in with_ex["episodes"][0]["attempts"])
    assert SECRET not in json.dumps(with_ex)


def test_round_trip(app: App, tmp_path) -> None:
    load(
        app,
        episode("rt1", 0, "KeyError: 'x'", "add default"),
        episode("rt2", 10, "TypeError: y", "cast to int", verified=False),
    )
    b1 = export_bundle(app.store, app.settings.workspace, include_excerpts=True)
    other = App(
        Settings(home=tmp_path / "other", workspace=app.settings.workspace, embedding_provider="hashing"),
        provider=HashingProvider(),
    )
    counts = import_bundle(other.store, b1)
    assert counts["episodes"] == 2 and counts["skipped"] == 0
    b2 = export_bundle(other.store, app.settings.workspace, include_excerpts=True)
    assert _strip_volatile(b1) == _strip_volatile(b2)
    assert import_bundle(other.store, b1)["skipped"] == 2  # idempotent


def test_invalid_bundles_rejected(app: App) -> None:
    load(app, episode("bad", 0, "KeyError: 'x'", "add default"))
    b = export_bundle(app.store, app.settings.workspace)
    broken = copy.deepcopy(b)
    broken["episodes"][0]["outcome"]["status"] = "fixed"
    with pytest.raises(BundleValidationError):
        validate(broken)
    broken = copy.deepcopy(b)
    broken["redaction"]["applied"] = False
    with pytest.raises(BundleValidationError):
        validate(broken)
    broken = copy.deepcopy(b)
    broken["episodes"][0]["lessons"][0]["source_episode_ids"] = []
    with pytest.raises(BundleValidationError):
        validate(broken)
