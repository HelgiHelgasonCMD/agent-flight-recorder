"""Tool/command categorization. Uses only the tool name and the command's leading tokens."""

from __future__ import annotations

import re
import shlex

_TEST = re.compile(
    r"^(pytest|py\.test|tox|nox|jest|vitest|mocha|ava|rspec|phpunit|ctest)\b"
    r"|^(npm|pnpm|yarn|bun)\s+(run\s+)?test\b"
    r"|^(go|cargo|dotnet|mix|swift|deno)\s+test\b"
    r"|^(python[0-9.]*|uv run|poetry run)\s+(-m\s+)?(pytest|unittest)\b"
    r"|^(make|just)\s+(test|check)\b"
    r"|^(mvn|gradle|\./gradlew)\s+.*\btest\b"
)
_INSTALL = re.compile(
    r"^(pip[0-9.]*|uv\s+(pip|add|sync)|poetry\s+(add|install)|npm\s+(i|install|ci)|pnpm\s+(i|install|add)"
    r"|yarn(\s+add|\s+install)?$|cargo\s+add|go\s+(get|mod)|apt(-get)?|brew|conda|gem|bundle)\b"
)
_BUILD = re.compile(
    r"^(make|cmake|cargo\s+build|go\s+build|tsc|npm\s+run\s+build|gradle|mvn|docker\s+build|webpack|vite\s+build)\b"
)
_LINT = re.compile(r"^(ruff|flake8|pylint|eslint|prettier|black|isort|golangci-lint|clippy|cargo\s+clippy)\b")
_TYPECHECK = re.compile(r"^(mypy|pyright|tsc\s+--noEmit|npx\s+tsc)\b")
_VCS = re.compile(r"^(git|gh|hg)\b")

_TOOL_CATEGORIES = {
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "Read": "read",
    "Glob": "search",
    "Grep": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "agent",
    "Agent": "agent",
}


def _strip_env_prefix(command: str) -> str:
    # drop leading VAR=value assignments and common wrappers
    cmd = command.strip()
    prev = None
    while prev != cmd:
        prev = cmd
        cmd = re.sub(r"^cd\s+\S+\s*(?:&&|;)\s*", "", cmd)
        cmd = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+", "", cmd)
        cmd = re.sub(r"^(?:sudo|time|nice|env)\s+", "", cmd)
    return cmd


def program_of(command: str) -> str | None:
    cmd = _strip_env_prefix(command)
    try:
        parts = shlex.split(cmd, posix=True)
    except ValueError:
        parts = cmd.split()
    if not parts:
        return None
    prog = parts[0].rsplit("/", 1)[-1]
    return prog[:64]


def categorize(tool_name: str, command: str | None = None) -> str:
    if tool_name.startswith("mcp__"):
        return "mcp"
    if tool_name in _TOOL_CATEGORIES:
        return _TOOL_CATEGORIES[tool_name]
    if tool_name in {"Bash", "PowerShell", "shell", "exec"} or command:
        cmd = _strip_env_prefix(command or "")
        for name, pattern in (
            ("test", _TEST),
            ("typecheck", _TYPECHECK),
            ("lint", _LINT),
            ("install", _INSTALL),
            ("build", _BUILD),
            ("vcs", _VCS),
        ):
            if pattern.search(cmd):
                return name
        return "shell"
    return "other"


def normalize_command(command: str) -> str:
    """Canonical form used to detect re-runs of the same command."""
    return re.sub(r"\s+", " ", _strip_env_prefix(command)).strip()
