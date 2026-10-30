"""Objective verification.

Task verification (did the task's check pass in a known state?) and root-cause
verification (was a specific explanation checked by a discriminating test?) are recorded
and propagated separately. A passing test suite never upgrades a root-cause claim.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from afr.domain.models import (
    OutcomeStatus,
    RootCauseStatus,
    VerificationEvidence,
    VerificationKind,
    VerificationScope,
)
from afr.episodes import fingerprint
from afr.ingest.pipeline import Pipeline, new_id
from afr.persistence.db import with_retry
from afr.redaction import sanitize

DEFAULT_TIMEOUT_S = 900


@dataclass
class CommandRun:
    exit_code: int | None
    duration_s: float
    timed_out: bool
    output_tail: str


def run_command(argv: list[str], cwd: Path, timeout_s: int = DEFAULT_TIMEOUT_S) -> CommandRun:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stdout or b"")[-2000:] if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-2000:]
        return CommandRun(None, time.monotonic() - t0, True, str(tail))
    out = (proc.stdout or "") + (proc.stderr or "")
    return CommandRun(proc.returncode, time.monotonic() - t0, False, out[-2000:])


def record(pipeline: Pipeline, ev: VerificationEvidence, lesson_id: str | None = None) -> VerificationEvidence:
    store = pipeline.store

    def _tx() -> None:
        with store.engine.begin() as conn:
            ep = store.get_episode(ev.episode_id, conn)
            if ep is None:
                raise KeyError(f"unknown episode {ev.episode_id}")
            if not ev.failure_signature_ids:
                ev.failure_signature_ids = list(
                    dict.fromkeys(
                        e.signature_id for e in store.events_for_episode(ep.episode_id, conn) if e.signature_id
                    )
                )
            store.add_verification(conn, ev)
            if ev.scope == VerificationScope.TASK:
                status = OutcomeStatus.VERIFIED_SUCCESS if ev.passed else OutcomeStatus.VERIFIED_FAILURE
                store.update_episode(conn, ep.episode_id, status=status)
            elif ev.passed and ev.claim:
                store.update_episode(conn, ep.episode_id, root_cause_status=RootCauseStatus.VERIFIED)
            pipeline.refresh_auto_lesson(conn, ep.episode_id)

    with_retry(_tx)

    # propagate to lessons that cite this episode
    ep = store.get_episode(ev.episode_id)
    assert ep is not None
    for les in store.lessons_for_episode(ev.episode_id):
        changed = False
        if ev.evidence_id not in les.verification_ids:
            les.verification_ids.append(ev.evidence_id)
            changed = True
        if ev.scope == VerificationScope.TASK and les.task_status != ep.status:
            les.task_status = ep.status
            if ev.passed:
                les.verified_at = ev.ts
            changed = True
        if (
            ev.scope == VerificationScope.ROOT_CAUSE
            and ev.passed
            and ev.claim
            and (lesson_id is None or lesson_id == les.lesson_id)
            and les.root_cause
            and ev.claim.strip() == les.root_cause.strip()
        ):
            les.root_cause_status = RootCauseStatus.VERIFIED
            changed = True
        if changed:
            les.updated_at = datetime.now(UTC)
            store.upsert_lesson(les)
    return ev


def verify_with_command(
    pipeline: Pipeline,
    episode_id: str,
    argv: list[str],
    cwd: Path,
    scope: VerificationScope = VerificationScope.TASK,
    claim: str | None = None,
    lesson_id: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[VerificationEvidence, CommandRun]:
    if scope == VerificationScope.ROOT_CAUSE and not claim:
        raise ValueError("root-cause verification needs the explanation being checked (--claim)")
    env_fp = fingerprint.capture(cwd)
    run = run_command(argv, cwd, timeout_s)
    after_fp = fingerprint.capture(cwd)
    note = f"ran by afr verify in {run.duration_s:.1f}s"
    if run.timed_out:
        note += f"; timed out after {timeout_s}s"
    if env_fp.worktree_fingerprint != after_fp.worktree_fingerprint:
        note += "; working tree changed while the command ran (fingerprint is the pre-run state)"
    shown = [Path(argv[0]).name if Path(argv[0]).is_absolute() else argv[0], *argv[1:]]
    cmd_text = sanitize(" ".join(shown), 400).text
    ev = VerificationEvidence(
        evidence_id=new_id("ver"),
        episode_id=episode_id,
        kind=VerificationKind.TEST_COMMAND,
        scope=scope,
        command=cmd_text,
        exit_code=run.exit_code,
        passed=run.exit_code == 0,
        ts=datetime.now(UTC),
        environment=env_fp,
        claim=sanitize(claim, 500).text if claim else None,
        note=note,
    )
    return record(pipeline, ev, lesson_id), run


def current_workdir() -> Path:
    return Path(os.getcwd())
