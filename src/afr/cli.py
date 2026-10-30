"""``afr`` command-line interface."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from afr.config import CaptureLevel, Settings

if TYPE_CHECKING:
    from afr.app import App

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Agent Flight Recorder: record agent failures, verify fixes, retrieve verified experience.",
)
hook_app = typer.Typer(no_args_is_help=True, help="Claude Code hook integration (project scope only).")
episode_app = typer.Typer(no_args_is_help=True, help="Inspect episodes (task attempts).")
lesson_app = typer.Typer(no_args_is_help=True, help="Inspect and curate lessons.")
index_app = typer.Typer(no_args_is_help=True, help="Manage the derived Qdrant index.")
mcp_app = typer.Typer(no_args_is_help=True, help="MCP stdio server.")
eval_app = typer.Typer(help="Retrieval benchmark and optional live-agent evaluation.", invoke_without_command=True)
app.add_typer(hook_app, name="hook")
app.add_typer(episode_app, name="episode")
app.add_typer(lesson_app, name="lesson")
app.add_typer(index_app, name="index")
app.add_typer(mcp_app, name="mcp")
app.add_typer(eval_app, name="eval")

_state: dict[str, Any] = {}

HomeOpt = Annotated[
    Path | None,
    typer.Option("--home", help="Data directory (default: $AFR_HOME or ~/.local/share/agent-flight-recorder)."),
]
WorkspaceOpt = Annotated[
    str | None,
    typer.Option("--workspace", "-w", help="Workspace/tenant boundary (default: $AFR_WORKSPACE or 'default')."),
]


@app.callback()
def _main(home: HomeOpt = None, workspace: WorkspaceOpt = None) -> None:
    _state["settings"] = Settings.from_env(home=home, workspace=workspace)
    _state.pop("app", None)


def settings() -> Settings:
    s: Settings = _state.get("settings") or Settings.from_env()
    return s


def get_app() -> App:
    from afr.app import App

    if "app" not in _state:
        _state["app"] = App(settings())
    a: App = _state["app"]
    return a


def _echo_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _style(text: str, fg: str | None = None, bold: bool = False) -> str:
    return typer.style(text, fg=fg, bold=bold)


# --------------------------------------------------------------------------- doctor
@app.command()
def doctor(
    no_index: Annotated[bool, typer.Option("--no-index", help="Skip the Qdrant/embedding check.")] = False,
    project: Annotated[Path, typer.Option(help="Project to inspect for installed hooks.")] = Path("."),
) -> None:
    """Check storage, ingestion counters, spool/dropped events, index lag and hooks."""
    from afr import doctor as doc

    checks = doc.run(get_app(), check_index=not no_index, project=project)
    bad = 0
    for c in checks:
        mark = _style("ok ", "green") if c.ok else _style("!! ", "red", bold=True)
        bad += 0 if c.ok else 1
        typer.echo(f"{mark} {c.name:<18} {c.detail}")
    if bad:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- ingest
@app.command()
def ingest(
    path: Annotated[Path | None, typer.Argument(help="JSONL file in afr.generic-event.v1 format.")] = None,
    level: Annotated[
        CaptureLevel, typer.Option(help="Capture level for imported text.")
    ] = CaptureLevel.SANITIZED_EVIDENCE,
    replay_spool: Annotated[
        bool, typer.Option("--replay-spool", help="Re-ingest hook events that were spooled after a failure.")
    ] = False,
) -> None:
    """Import events (generic JSONL) or replay the hook dead-letter spool."""
    a = get_app()
    if replay_spool:
        from afr.adapters.claude_code.mapping import to_raw
        from afr.ingest.spool import drain, spool_event

        ok = failed = skipped = 0
        for rec in drain(a.settings.spool_path):
            payload = rec.get("payload") or {}
            raw = to_raw(payload) if isinstance(payload, dict) else None
            if raw is None:
                skipped += 1
                continue
            try:
                a.pipeline.ingest(raw, level)
                ok += 1
            except Exception as exc:
                failed += 1
                spool_event(a.settings.spool_path, a.settings.spool_max_bytes, payload, f"replay: {exc}")
        typer.echo(f"replayed={ok} failed_and_respooled={failed} unreplayable={skipped}")
        return
    if path is None:
        raise typer.BadParameter("give a JSONL file or --replay-spool")
    from afr.adapters.generic_jsonl import import_file

    rep = import_file(a.pipeline, path, level, a.settings.workspace)
    typer.echo(
        f"lines={rep.lines} stored={rep.stored} deduped={rep.deduped} "
        f"verifications={rep.verifications} lessons={rep.lessons} invalid={len(rep.invalid)}"
    )
    for msg in rep.invalid[:20]:
        typer.echo(_style(f"  invalid {msg}", "yellow"))
    if rep.invalid:
        raise typer.Exit(2)


# --------------------------------------------------------------------------- search
def _parse_kv(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise typer.BadParameter(f"expected name=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def print_hits(result: Any, verbose: bool = True) -> None:
    typer.echo(
        _style(f"mode={result.mode} latency={result.latency_ms:.0f}ms  ", "cyan")
        + _style(result.notice, "bright_black")
    )
    if not result.hits:
        typer.echo("no matching experience")
    for h in result.hits:
        status_color = "green" if h.task_status == "verified_success" else "yellow"
        typer.echo(
            f"\n#{h.rank} "
            + _style(h.lesson_id or h.doc_id, bold=True)
            + f"  [{_style(h.task_status, status_color)}; root cause {h.root_cause_status}; {h.origin}; tier {h.tier}]"
        )
        typer.echo(f"   problem : {h.title}")
        if h.fix:
            typer.echo(f"   fix     : {h.fix}")
        for f in h.failed_approaches[:2]:
            typer.echo(f"   failed  : {f}")
        typer.echo(f"   why     : {h.reason}")
        typer.echo(f"   episodes: {', '.join(h.episode_ids)}")
        for e in h.evidence[:2]:
            rev = (e.get("git_head") or "")[:10]
            typer.echo(
                f"   evidence: {e['evidence_id']} {e['kind']}/{e['scope']} `{e.get('command') or ''}` "
                f"exit={e.get('exit_code')} passed={e['passed']} rev={rev or '-'} at {e['ts'][:19]}"
            )
        for p in h.prerequisites[:4]:
            typer.echo(f"   requires: {p}")
        for c in h.cautions:
            typer.echo(_style(f"   caution : {c['code']}: {c['detail']}", "red" if h.tier >= 3 else "yellow"))


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Error text or task description.")],
    mode: Annotated[str, typer.Option(help="bm25 | dense | hybrid")] = "hybrid",
    k: Annotated[int, typer.Option("-k")] = 5,
    repo: Annotated[str | None, typer.Option(help="Filter: repo id.")] = None,
    language: Annotated[str | None, typer.Option()] = None,
    tool: Annotated[str | None, typer.Option(help="Filter: tool category (test, install, ...).")] = None,
    error_type: Annotated[str | None, typer.Option(help="Filter: exception type or error code.")] = None,
    version: Annotated[str | None, typer.Option(help="Filter: exact recorded version name=value.")] = None,
    since_days: Annotated[int | None, typer.Option(help="Filter: only experience from the last N days.")] = None,
    verified_only: Annotated[bool, typer.Option("--verified-only")] = False,
    current_os: Annotated[str | None, typer.Option("--os", help="Current OS, for applicability checks.")] = None,
    current: Annotated[
        list[str] | None, typer.Option("--current", help="Current version name=value (repeatable).")
    ] = None,
    current_repo: Annotated[str | None, typer.Option(help="Current repo id, for cross-project cautions.")] = None,
    allow_workspace: Annotated[
        list[str] | None, typer.Option("--allow-workspace", help="Explicitly also search this workspace.")
    ] = None,
    no_policy: Annotated[bool, typer.Option("--no-policy", help="Raw retrieval order (ablation).")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search recorded experience (workspace-scoped)."""
    from afr.retrieval.policy import QueryContext
    from afr.retrieval.search import SearchRequest

    if mode not in ("bm25", "dense", "hybrid"):
        raise typer.BadParameter("mode must be bm25, dense or hybrid")
    a = get_app()
    req = SearchRequest(
        query=query,
        workspace=a.settings.workspace,
        mode=mode,  # type: ignore[arg-type]
        k=k,
        allow_workspaces=allow_workspace or [],
        repo_id=repo,
        language=language,
        tool=tool,
        error_type=error_type,
        version=version,
        since=datetime.now(UTC) - timedelta(days=since_days) if since_days else None,
        task_status=["verified_success"] if verified_only else [],
        apply_policy=not no_policy,
        context=QueryContext(repo_id=current_repo, os=current_os, versions=_parse_kv(current or [])),
    )
    res = a.search(req)
    if as_json:
        _echo_json(
            {
                "notice": res.notice,
                "mode": res.mode,
                "latency_ms": res.latency_ms,
                "hits": [h.as_dict() for h in res.hits],
            }
        )
    else:
        print_hits(res)


# --------------------------------------------------------------------------- episodes
@episode_app.command("list")
def episode_list(limit: Annotated[int, typer.Option()] = 50) -> None:
    """List episodes in the workspace."""
    a = get_app()
    for ep in a.store.list_episodes(a.settings.workspace)[-limit:]:
        typer.echo(
            f"{ep.episode_id}  {ep.started_at:%Y-%m-%d %H:%M}  {ep.status:<17} {ep.evidence_level:<18} {ep.task_summary or '-'}"
        )


@episode_app.command("show")
def episode_show(episode_id: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show an episode with its events, signatures, verifications and lessons."""
    a = get_app()
    ep = a.store.get_episode(episode_id)
    if ep is None or ep.workspace != a.settings.workspace:
        typer.echo(f"episode {episode_id} not found in workspace {a.settings.workspace}", err=True)
        raise typer.Exit(1)
    if as_json:
        st = a.store
        events = st.events_for_episode(episode_id)
        sigs = st.get_signatures(e.signature_id for e in events if e.signature_id)
        _echo_json(
            {
                "episode": ep.model_dump(mode="json"),
                "events": [e.model_dump(mode="json") for e in events],
                "signatures": [sg.model_dump(mode="json") for sg in sigs],
                "verifications": [v.model_dump(mode="json") for v in st.verifications_for(episode_id)],
                "lessons": [les.model_dump(mode="json") for les in st.lessons_for_episode(episode_id)],
            }
        )
        return
    render_episode(a, episode_id)


def render_episode(a: App, episode_id: str) -> None:
    st = a.store
    ep = st.get_episode(episode_id)
    assert ep is not None
    events = st.events_for_episode(episode_id)
    sigs = {sg.signature_id: sg for sg in st.get_signatures(e.signature_id for e in events if e.signature_id)}
    vers = st.verifications_for(episode_id)
    lessons = st.lessons_for_episode(episode_id)
    typer.echo(_style(f"episode {ep.episode_id}", bold=True) + f"  session {ep.session_id}  agent {ep.agent}")
    typer.echo(f"  task      : {ep.task_summary or '-'}")
    typer.echo(f"  outcome   : {ep.status}   root cause: {ep.root_cause_status}   evidence level: {ep.evidence_level}")
    typer.echo(f"  env       : repo={ep.repo_id} head={ep.environment.git_head} versions={ep.environment.versions}")
    typer.echo("  events:")
    for e in events:
        tool = e.tool
        desc = ""
        if tool:
            desc = f"{tool.tool_name}/{tool.tool_category} " + (
                f"`{tool.command}`" if tool.command else (tool.program or "")
            )
            if tool.files:
                desc += " " + ", ".join(tool.files[:3])
            if tool.exit_code is not None:
                desc += f" exit={tool.exit_code}"
        typer.echo(f"    {e.ts:%H:%M:%S} {e.event_id} {e.kind:<12} {desc}")
        sig = sigs.get(e.signature_id or "")
        if sig:
            label = sig.exception_type or sig.error_code or ""
            typer.echo(f"{'':>8}↳ {sig.signature_id} {label}: {sig.normalized_message[:100]}")
    typer.echo("  verifications:")
    for v in vers:
        typer.echo(
            f"    {v.evidence_id} {v.kind}/{v.scope} `{v.command}` exit={v.exit_code} passed={v.passed} "
            f"head={(v.environment.git_head or '-')[:10]} tree={v.environment.worktree_fingerprint or '-'}"
        )
    if not vers:
        typer.echo("    (none: outcome is not objectively verified)")
    typer.echo("  lessons: " + (", ".join(f"{les.lesson_id} ({les.origin}, {les.status})" for les in lessons) or "-"))


@episode_app.command("annotate")
def episode_annotate(
    episode_id: str, summary: Annotated[str, typer.Option(help="Task summary (redacted before storage).")]
) -> None:
    """Attach a human-written task summary to an episode."""
    from afr.redaction import sanitize

    a = get_app()
    with a.store.engine.begin() as conn:
        a.store.update_episode(conn, episode_id, task_summary=sanitize(summary, 500).text)
        a.pipeline.refresh_auto_lesson(conn, episode_id)
    typer.echo("ok")


@episode_app.command("delete")
def episode_delete(episode_id: str, yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Delete an episode, its events and verifications (lessons citing only it are removed)."""
    if not yes:
        typer.confirm(f"Delete episode {episode_id} and its evidence?", abort=True)
    counts = get_app().store.delete_episode(episode_id)
    typer.echo(json.dumps(counts))


# --------------------------------------------------------------------------- verify
@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def verify(
    ctx: typer.Context,
    episode_id: str,
    scope: Annotated[str, typer.Option(help="task | root-cause")] = "task",
    claim: Annotated[
        str | None, typer.Option(help="Root-cause explanation this check discriminates (root-cause scope).")
    ] = None,
    lesson: Annotated[str | None, typer.Option(help="Lesson whose root cause is being checked.")] = None,
    cwd: Annotated[Path, typer.Option(help="Directory to run the command in.")] = Path("."),
    timeout: Annotated[int, typer.Option(help="Seconds.")] = 900,
) -> None:
    """Run a check command and record its exit code as verification evidence.

    Usage: afr verify EPISODE_ID -- python -m pytest tests/test_x.py
    """
    from afr.domain.models import VerificationScope
    from afr.verification import verify_with_command

    argv = list(ctx.args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise typer.BadParameter("give the check command after --")
    sc = VerificationScope.ROOT_CAUSE if scope.replace("-", "_") == "root_cause" else VerificationScope.TASK
    a = get_app()
    ev, _run = verify_with_command(a.pipeline, episode_id, argv, cwd.resolve(), sc, claim, lesson, timeout)
    color = "green" if ev.passed else "red"
    typer.echo(
        _style(f"{'PASSED' if ev.passed else 'FAILED'}", color, bold=True)
        + f" exit={ev.exit_code} evidence={ev.evidence_id} scope={ev.scope} head={(ev.environment.git_head or '-')[:10]} "
        f"tree={ev.environment.worktree_fingerprint or '-'}"
    )
    ep = a.store.get_episode(episode_id)
    assert ep is not None
    typer.echo(f"episode {episode_id}: outcome={ep.status} root_cause={ep.root_cause_status}")
    if not ev.passed:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- lessons
@lesson_app.command("list")
def lesson_list(all_: Annotated[bool, typer.Option("--all", help="Include superseded/invalidated.")] = False) -> None:
    from afr.domain.models import LessonStatus

    a = get_app()
    statuses = None if all_ else [LessonStatus.ACTIVE]
    for les in a.store.list_lessons(a.settings.workspace, statuses):
        typer.echo(f"{les.lesson_id}  {les.status:<11} {les.origin:<18} task={les.task_status:<17} {les.problem[:80]}")


@lesson_app.command("show")
def lesson_show(lesson_id: str) -> None:
    les = get_app().store.get_lesson(lesson_id)
    if les is None:
        typer.echo(f"lesson {lesson_id} not found", err=True)
        raise typer.Exit(1)
    _echo_json(les.model_dump(mode="json"))


@lesson_app.command("add")
def lesson_add(
    episode: Annotated[list[str], typer.Option(help="Source episode id (repeatable).")],
    problem: Annotated[str, typer.Option()],
    fix: Annotated[str | None, typer.Option(help="What actually worked.")] = None,
    symptom: Annotated[list[str] | None, typer.Option()] = None,
    failed: Annotated[list[str] | None, typer.Option(help="Approach that did not work (repeatable).")] = None,
    root_cause: Annotated[str | None, typer.Option()] = None,
    precondition: Annotated[list[str] | None, typer.Option()] = None,
    applies_when: Annotated[list[str] | None, typer.Option(help="name=spec, e.g. os=linux or python=>=3.12")] = None,
    contraindication: Annotated[list[str] | None, typer.Option()] = None,
    supersedes: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Add a reviewed lesson by hand (its root cause stays 'supported' until a discriminating check is recorded)."""
    from afr.domain.models import RootCauseStatus
    from afr.lessons.service import add_manual_lesson

    les = add_manual_lesson(
        get_app().store,
        episode_ids=episode,
        problem=problem,
        successful_approach=fix,
        observed_symptoms=symptom or [],
        failed_approaches=failed or [],
        root_cause=root_cause,
        root_cause_status=RootCauseStatus.SUPPORTED if root_cause else RootCauseStatus.UNVERIFIED,
        preconditions=precondition or [],
        applies_when=_parse_kv(applies_when or []),
        contraindications=contraindication or [],
        supersedes=supersedes or [],
    )
    typer.echo(
        f"{les.lesson_id} task={les.task_status} root_cause={les.root_cause_status} basis: {les.confidence_basis}"
    )


@lesson_app.command("invalidate")
def lesson_invalidate(lesson_id: str, reason: Annotated[str, typer.Option()]) -> None:
    from afr.lessons.service import invalidate

    les = invalidate(get_app().store, lesson_id, reason)
    typer.echo(f"{les.lesson_id} invalidated")


@lesson_app.command("supersede")
def lesson_supersede(old_id: str, new_id: str) -> None:
    from afr.lessons.service import supersede

    supersede(get_app().store, old_id, new_id)
    typer.echo(f"{old_id} superseded by {new_id}")


# --------------------------------------------------------------------------- export/import
@app.command()
def export(
    output: Annotated[Path, typer.Option("-o", "--output")],
    episode: Annotated[list[str] | None, typer.Option(help="Only these episodes.")] = None,
    include_excerpts: Annotated[
        bool, typer.Option("--include-excerpts", help="Include redacted error excerpts and command lines.")
    ] = False,
) -> None:
    """Export a validated experience-bundle.v1 JSON file."""
    from afr.export.bundle import export_bundle

    a = get_app()
    bundle = export_bundle(a.store, a.settings.workspace, episode, include_excerpts)
    output.write_text(json.dumps(bundle, indent=2) + "\n")
    n_les = sum(len(e["lessons"]) for e in bundle["episodes"])
    typer.echo(
        f"wrote {output}: {len(bundle['episodes'])} episodes, {n_les} lessons, schema valid, excerpts={'yes' if include_excerpts else 'no'}"
    )


@app.command("import-bundle")
def import_bundle_cmd(path: Path) -> None:
    """Import an experience-bundle.v1 file (validated first)."""
    from afr.export.bundle import import_bundle

    a = get_app()
    counts = import_bundle(a.store, json.loads(path.read_text()), a.settings.workspace)
    typer.echo(json.dumps(counts))


@app.command()
def purge(
    older_than: Annotated[int | None, typer.Option(help="Days; defaults to $AFR_RETENTION_DAYS.")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Apply retention: delete episodes older than N days."""
    a = get_app()
    days = older_than or a.settings.retention_days
    if not days:
        raise typer.BadParameter("give --older-than or set AFR_RETENTION_DAYS")
    ids = a.store.episodes_before(datetime.now(UTC) - timedelta(days=days))
    if not ids:
        typer.echo("nothing to purge")
        return
    if not yes:
        typer.confirm(f"Delete {len(ids)} episode(s) older than {days} days?", abort=True)
    for eid in ids:
        a.store.delete_episode(eid)
    typer.echo(f"purged {len(ids)} episode(s); run `afr index sync` to drop them from the index")


# --------------------------------------------------------------------------- index
@index_app.command("sync")
def index_sync() -> None:
    """Upsert changed documents into Qdrant (idempotent)."""
    r = get_app().sync_index()
    typer.echo(
        f"collection={r.collection} docs={r.total_docs} upserted={r.upserted} deleted={r.deleted} unchanged={r.unchanged}"
    )


@index_app.command("rebuild")
def index_rebuild() -> None:
    """Drop and rebuild the collection from SQLite."""
    r = get_app().sync_index(rebuild=True)
    typer.echo(f"rebuilt collection={r.collection} docs={r.total_docs} avgdl={r.avgdl:.1f}")


# --------------------------------------------------------------------------- hooks
@hook_app.command("install")
def hook_install(
    scope: Annotated[
        str, typer.Option(help="Only 'project' is supported; user-global config is never modified.")
    ] = "project",
    project: Annotated[Path, typer.Option(help="Project root.")] = Path("."),
    level: Annotated[
        CaptureLevel, typer.Option(help="metadata (default) or sanitized_evidence.")
    ] = CaptureLevel.METADATA,
    proactive: Annotated[
        bool, typer.Option("--proactive", help="Also surface verified experience to Claude on tool failures.")
    ] = False,
    shared: Annotated[
        bool, typer.Option("--shared", help="Write .claude/settings.json (committable) instead of settings.local.json.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the diff only.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Apply without the confirmation prompt.")] = False,
) -> None:
    """Install recording hooks into this project's Claude Code settings (shows a diff first)."""
    from afr.adapters.claude_code.installer import apply, is_gitignored, plan_install

    if scope != "project":
        typer.echo("only --scope project is supported; afr never edits user-global settings", err=True)
        raise typer.Exit(2)
    s = settings()
    # a personal settings file pins the data dir; a shared (committed) one must not embed local paths
    home = None if shared else s.home
    plan = plan_install(project.resolve(), level, s.workspace, proactive, shared, home)
    if not plan.changed:
        typer.echo(f"{plan.path}: already up to date")
        return
    typer.echo(plan.diff())
    if dry_run:
        typer.echo("(dry run: nothing written)")
        return
    if not yes:
        typer.confirm(f"Write {plan.path}?", abort=True)
    apply(plan)
    typer.echo(f"wrote {plan.path} (backup of the previous file: *.afr-backup)")
    if not shared and is_gitignored(project.resolve(), plan.path) is False:
        typer.echo(_style(f"note: {plan.path} is not git-ignored; add it to .gitignore to keep it personal", "yellow"))


@hook_app.command("uninstall")
def hook_uninstall(
    project: Annotated[Path, typer.Option()] = Path("."),
    shared: Annotated[bool, typer.Option("--shared")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove only afr's hook handlers, preserving everything else."""
    from afr.adapters.claude_code.installer import apply, plan_uninstall

    plan = plan_uninstall(project.resolve(), shared)
    if not plan.changed:
        typer.echo("no afr hooks installed")
        return
    typer.echo(plan.diff())
    if dry_run:
        typer.echo("(dry run: nothing written)")
        return
    if not yes:
        typer.confirm(f"Write {plan.path}?", abort=True)
    apply(plan)
    typer.echo(f"removed afr hooks from {plan.path}")


@hook_app.command("receive")
def hook_receive(
    level: Annotated[CaptureLevel, typer.Option()] = CaptureLevel.METADATA,
    proactive: Annotated[bool, typer.Option("--proactive")] = False,
    workspace: WorkspaceOpt = None,
    home: HomeOpt = None,
) -> None:
    """(Called by Claude Code.) Read one hook event from stdin. Always exits 0."""
    from afr.adapters.claude_code.receiver import main

    try:
        s = Settings.from_env(home=home or settings().home, workspace=workspace or settings().workspace)
    except Exception:
        s = settings()
    raise typer.Exit(main(level, proactive, s))


# --------------------------------------------------------------------------- mcp
@mcp_app.command("serve")
def mcp_serve(
    allow_workspace: Annotated[
        list[str] | None, typer.Option("--allow-workspace", help="Explicitly expose another workspace too.")
    ] = None,
) -> None:
    """Run the MCP server on stdio."""
    from afr.mcp_server.server import serve

    serve(get_app(), allow_workspace or [])


# --------------------------------------------------------------------------- feedback
@app.command("feedback")
def feedback_list() -> None:
    """List retrieval feedback (consulted lessons vs objective outcomes)."""
    a = get_app()
    for fb in a.store.list_feedback(a.settings.workspace):
        typer.echo(
            f"{fb.feedback_id} task={fb.task_ref} consulted={fb.consulted_lesson_ids} "
            f"objective={fb.outcome} self_report={fb.reported_outcome}"
        )


# --------------------------------------------------------------------------- demo / eval
@app.command()
def demo(
    scenario: Annotated[str, typer.Option(help="repeat | trap | all")] = "all",
    home: Annotated[
        Path | None, typer.Option("--demo-home", help="Directory for demo data (default: a fresh temp dir).")
    ] = None,
    embedding: Annotated[
        str | None, typer.Option(help="Embedding provider override (sentence-transformers | hashing).")
    ] = None,
) -> None:
    """Deterministic end-to-end demo in an isolated data directory."""
    from afr.demo import run_demo

    raise typer.Exit(run_demo(scenario, home, embedding))


@eval_app.callback()
def eval_main(
    ctx: typer.Context,
    modes: Annotated[str, typer.Option(help="Comma-separated: bm25,dense,hybrid")] = "bm25,dense,hybrid",
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write a markdown report here.")] = None,
    json_out: Annotated[Path | None, typer.Option("--json-out")] = None,
    embedding: Annotated[str | None, typer.Option(help="Embedding provider override.")] = None,
    corpus: Annotated[Path | None, typer.Option(help="Corpus directory (default: benchmarks/corpus).")] = None,
) -> None:
    """Run the offline retrieval benchmark (synthetic fixtures; not a live-agent result)."""
    if ctx.invoked_subcommand is not None:
        return
    from afr.eval.harness import run_benchmark

    raise typer.Exit(
        run_benchmark([m.strip() for m in modes.split(",") if m.strip()], output, json_out, embedding, corpus)
    )


@eval_app.command("live")
def eval_live(
    tasks: Annotated[Path, typer.Option(help="Paired task file (see benchmarks/live/README.md).")] = Path(
        "benchmarks/live/tasks.json"
    ),
    agent: Annotated[str, typer.Option()] = "claude",
    i_understand_cost: Annotated[
        bool, typer.Option("--i-understand-cost", help="Required: runs a real agent with your credentials.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan the paired runs without invoking any agent.")
    ] = False,
    output: Annotated[Path, typer.Option("-o")] = Path("live-eval-results.json"),
) -> None:
    """Optional paired live-agent evaluation (retrieval vs no-retrieval control)."""
    from afr.eval.live import run_live

    raise typer.Exit(run_live(tasks, agent, i_understand_cost, dry_run, output))


def main() -> None:  # pragma: no cover
    app(prog_name="afr")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
