from afr.episodes.signatures import build_signature
from afr.episodes.tools import categorize, normalize_command, program_of

PY_TB = """Traceback (most recent call last):
  File "/home/a/proj/app/main.py", line 3, in <module>
    import yaml
ModuleNotFoundError: No module named 'yaml'
"""


def test_python_traceback_signature() -> None:
    sig = build_signature("test", PY_TB)
    assert sig.exception_type == "ModuleNotFoundError"
    assert sig.resource_type == "module"
    assert "No module named 'yaml'" in sig.normalized_message


def test_volatile_tokens_normalized_same_signature() -> None:
    a = build_signature(
        "test", 'File "/home/a/x.py", line 10\nKeyError: 0x7f3a9c\nValueError: bad value at /tmp/abc/run.py:12:4'
    )
    b = build_signature(
        "test", 'File "/srv/b/y.py", line 99\nKeyError: 0x1b2c3d\nValueError: bad value at /var/tmp/q.py:88:1'
    )
    assert a.signature_id == b.signature_id


def test_distinct_errors_distinct_signatures() -> None:
    a = build_signature("test", "ModuleNotFoundError: No module named 'yaml'")
    b = build_signature("test", "ModuleNotFoundError: No module named 'requests'")
    assert a.signature_id != b.signature_id


def test_node_and_errno_codes() -> None:
    sig = build_signature(
        "shell", "Error [ERR_REQUIRE_ESM]: require() of ES Module /app/node_modules/x/index.js not supported."
    )
    assert sig.error_code == "ERR_REQUIRE_ESM"
    sig2 = build_signature("shell", "Error: listen EADDRINUSE: address already in use :::3000")
    assert sig2.error_code == "EADDRINUSE" and sig2.resource_type == "network_port"


def test_tool_categories() -> None:
    assert categorize("Bash", "python -m pytest tests/ -x") == "test"
    assert categorize("Bash", "FOO=1 npm test") == "test"
    assert categorize("Bash", "pip install -r requirements.txt") == "install"
    assert categorize("Bash", "git status") == "vcs"
    assert categorize("Edit") == "edit"
    assert categorize("mcp__server__tool") == "mcp"
    assert program_of("cd /x && FOO=bar /usr/bin/python3 app.py") == "python3"
    assert normalize_command("  pytest   -q  ") == "pytest -q"
