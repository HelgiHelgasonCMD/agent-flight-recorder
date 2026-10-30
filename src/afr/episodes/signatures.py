"""Failure signature normalization.

A signature groups failures that are "the same error" across runs: tool category,
exception type, error code, resource type and a normalized message with volatile
tokens (paths, line numbers, addresses, ids) removed. Version context is recorded
alongside but is not part of the identity, so the same error under two versions groups
together and version applicability is judged at retrieval time.
"""

from __future__ import annotations

import hashlib
import re

from afr.domain.models import FailureSignature

_EXC = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.)*([A-Z][A-Za-z0-9]*(?:Error|Exception|Exit|Fault|Interrupt))\b")
_ERRNO = (
    "ENOENT|EACCES|EPERM|EADDRINUSE|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|EEXIST|"
    "ENOSPC|EMFILE|ENOTDIR|EISDIR|EPIPE|ERESOLVE|ELIFECYCLE|EINTEGRITY|E404|E403|E401"
)
_CODES: list[re.Pattern[str]] = [
    re.compile(r"\berror\[(E\d{4})\]"),  # rustc
    re.compile(r"\b(ERR_[A-Z0-9_]{3,})\b"),  # node
    re.compile(rf"\b({_ERRNO})\b"),
    re.compile(r"\b(TS\d{4,5})\b"),  # typescript
    re.compile(r"\bnpm ERR! code ([A-Z0-9_]+)"),
    re.compile(r"\b(SQLSTATE\[?[0-9A-Z]{5}\]?)"),
    re.compile(r"\b(CS\d{4})\b"),  # C#
    re.compile(r"\[([a-z]+(?:-[a-z]+)+)\]\s*$", re.MULTILINE),  # mypy error codes like [attr-defined]
    re.compile(r"\b(?:HTTP(?:/\d\.\d)?|status(?:\s+code)?)\s*:?\s*([45]\d\d)\b", re.IGNORECASE),
]
_RESOURCES: list[tuple[str, re.Pattern[str]]] = [
    (
        "module",
        re.compile(
            r"No module named|Cannot find module|ModuleNotFoundError|could not resolve import|unresolved import|ImportError|ERR_REQUIRE_ESM|ERR_MODULE_NOT_FOUND",
            re.I,
        ),
    ),
    ("certificate", re.compile(r"\bSSL\b|certificate|CERT_", re.I)),
    ("network_port", re.compile(r"EADDRINUSE|address already in use|port \d+", re.I)),
    (
        "network",
        re.compile(
            r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|connection refused|Name or service not known|timed out", re.I
        ),
    ),
    ("permission", re.compile(r"EACCES|EPERM|Permission denied|PermissionError", re.I)),
    (
        "dependency",
        re.compile(
            r"ResolutionImpossible|ERESOLVE|version solving failed|conflicting dependencies|incompatible|peer dep", re.I
        ),
    ),
    ("file", re.compile(r"ENOENT|No such file|FileNotFoundError|not found", re.I)),
    (
        "syntax",
        re.compile(r"SyntaxError|IndentationError|Unexpected token|parse error|JSONDecodeError|Expecting", re.I),
    ),
    ("type", re.compile(r"TypeError|type error|incompatible type|\bTS\d{4}\b|\[[a-z]+-[a-z-]+\]", re.I)),
    ("memory", re.compile(r"MemoryError|out of memory|OOM|heap", re.I)),
    ("auth", re.compile(r"\b401\b|\b403\b|Unauthorized|Forbidden|authentication failed", re.I)),
    ("database", re.compile(r"sqlite|postgres|mysql|SQLSTATE|OperationalError|IntegrityError", re.I)),
]

_VOLATILE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<addr>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b"), "<hex>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"), "<ts>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[~.]?/[\w.@+-]+)+/?"), "<path>"),
    (re.compile(r"\bline \d+\b"), "line <n>"),
    (re.compile(r"\b(column|col|position|pos|char|offset|byte) \d+\b"), r"\1 <n>"),
    (re.compile(r":\d+(?::\d+)?\b"), ":<n>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|MB|KB|GB|B)\b"), "<qty>"),
]

_ERRORISH = re.compile(
    r"error|exception|failed|failure|fatal|cannot|can't|unable|denied|not found|refused|invalid|expecting", re.I
)


def headline(error_text: str) -> str:
    """The most informative single line of an error blob (last error-ish line wins,
    matching Python tracebacks and most CLI tools)."""
    lines = [ln.strip() for ln in error_text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^Exit code \d+$", ln)]
    exc_lines = [ln for ln in lines if _EXC.search(ln)]
    if exc_lines:
        return exc_lines[-1][:400]
    err_lines = [ln for ln in lines if _ERRORISH.search(ln)]
    if err_lines:
        return err_lines[-1][:400]
    return (lines[-1] if lines else "")[:400]


def normalize_message(line: str) -> str:
    out = line
    for pattern, repl in _VOLATILE:
        out = pattern.sub(repl, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:300]


def extract_code(text: str) -> str | None:
    for pattern in _CODES:
        m = pattern.search(text)
        if m:
            code = m.group(1)
            if pattern.pattern.startswith(r"\b(?:HTTP"):
                return f"HTTP_{code}"
            return code
    return None


def extract_exception(text: str) -> str | None:
    found = _EXC.findall(text)
    return found[-1] if found else None


def resource_type(text: str) -> str | None:
    for name, pattern in _RESOURCES:
        if pattern.search(text):
            return name
    return None


def build_signature(
    tool_category: str, error_text: str, version_context: dict[str, str] | None = None
) -> FailureSignature:
    """``error_text`` must already be redacted."""
    head = headline(error_text)
    exc = extract_exception(head) or extract_exception(error_text)
    code = extract_code(error_text)
    resource = resource_type(head) or resource_type(error_text)
    normalized = normalize_message(head) if head else "<no message>"
    key = "|".join([tool_category, exc or "", code or "", resource or "", normalized])
    sig_id = "sig_" + hashlib.sha256(key.encode()).hexdigest()[:16]
    return FailureSignature(
        signature_id=sig_id,
        tool_category=tool_category,
        exception_type=exc,
        error_code=code,
        resource_type=resource,
        normalized_message=normalized,
        version_context=dict(version_context or {}),
    )
