"""Environment and working-tree fingerprints.

The working-tree fingerprint hashes ``git status --porcelain`` plus the content of
``git diff HEAD`` and of untracked files. Only the hash is stored, never the diff, so
evidence can be tied to the exact tested state without persisting patches.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from pathlib import Path

from afr.domain.models import EnvironmentFingerprint

GIT_TIMEOUT_S = 5
_MAX_UNTRACKED_BYTES = 20_000_000


def _git(cwd: Path, *args: str) -> bytes | None:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def repo_root(cwd: Path) -> Path | None:
    out = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(out.decode().strip()) if out else None


def repo_identity(cwd: Path) -> str | None:
    """Stable repo id: hash of the normalized origin URL, else of the root path."""
    root = repo_root(cwd)
    if root is None:
        return None
    remote = _git(root, "config", "--get", "remote.origin.url")
    basis = remote.decode().strip().removesuffix(".git").lower() if remote else f"path:{root}"
    return "repo_" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def git_head(cwd: Path) -> str | None:
    out = _git(cwd, "rev-parse", "HEAD")
    return out.decode().strip() if out else None


def worktree_fingerprint(cwd: Path) -> tuple[str | None, bool | None]:
    root = repo_root(cwd)
    if root is None:
        return None, None
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    diff = _git(root, "diff", "HEAD", "--binary")
    if status is None:
        return None, None
    h = hashlib.sha256()
    h.update(status)
    h.update(diff or b"")
    budget = _MAX_UNTRACKED_BYTES
    for entry in status.split(b"\0"):
        if entry.startswith(b"?? "):
            p = root / entry[3:].decode(errors="replace")
            try:
                if p.is_file() and budget > 0:
                    data = p.read_bytes()[:budget]
                    budget -= len(data)
                    h.update(hashlib.sha256(data).digest())
            except OSError:
                continue
    return "wt_" + h.hexdigest()[:24], bool(status.strip())


def capture(cwd: Path, versions: dict[str, str] | None = None, with_worktree: bool = True) -> EnvironmentFingerprint:
    fp, dirty = worktree_fingerprint(cwd) if with_worktree else (None, None)
    return EnvironmentFingerprint(
        os=platform.system().lower(),
        repo_id=repo_identity(cwd),
        git_head=git_head(cwd),
        worktree_fingerprint=fp,
        worktree_dirty=dirty,
        versions=dict(versions or {}),
    )
