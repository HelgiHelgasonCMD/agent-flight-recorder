"""Secret redaction and size limiting.

Applied to every free-text field *before* it is persisted, embedded, indexed or exported.
Redaction is deliberately over-eager: a false positive costs a little retrieval quality,
a false negative leaks a credential.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

RULES_VERSION = "afr-redaction.v1"
MASK = "[REDACTED]"

# a pattern for secret *names*, not a secret
_SECRET_NAME = (
    r"[A-Za-z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY"  # noqa: S105
    r"|CREDENTIALS?|AUTH|SESSION[_-]?KEY|COOKIE)[A-Za-z0-9_.-]*"
)
_SECRET_NAME_RE = re.compile(rf"^{_SECRET_NAME}$", re.IGNORECASE)

# (name, pattern, replacement). Order matters: multi-line blocks first.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        MASK,
    ),
    (
        "auth_header",
        re.compile(
            r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key|cookie|set-cookie)(\s*[:=]\s*)"
            r"(?:(bearer|basic|token|digest)\s+)?[^\s,;\"']+"
        ),
        r"\1\2" + MASK,
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + MASK),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), MASK),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"), MASK),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"), MASK),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"), MASK),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MASK),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), MASK),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}"), MASK),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), MASK),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        MASK,
    ),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + MASK + "@"),
    (
        "secret_assignment",
        re.compile(rf"(?i)\b({_SECRET_NAME})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
        r"\1\2" + MASK,
    ),
    (
        "secret_flag",
        re.compile(r"(?i)(--?(?:password|passwd|token|api-key|apikey|secret|auth-token)(?:=|\s+))(\S+)"),
        r"\1" + MASK,
    ),
    # Long mixed-class strings look like generated credentials. Pure hex (git SHAs,
    # content hashes) is left alone because it is useful evidence and not a secret.
    (
        "high_entropy",
        re.compile(
            r"\b(?=[A-Za-z0-9+/_-]*[0-9])(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])[A-Za-z0-9+/_-]{32,}={0,2}"
        ),
        MASK,
    ),
]


@dataclass(frozen=True)
class Redacted:
    text: str
    count: int
    rules: tuple[str, ...]


def _secret_env_values(environ: Mapping[str, str]) -> list[str]:
    extra = {n.strip() for n in environ.get("AFR_REDACT_ENV", "").split(",") if n.strip()}
    values = []
    for name, value in environ.items():
        if len(value) < 6:
            continue
        if _SECRET_NAME_RE.match(name) or name in extra:
            values.append(value)
    # longest first so overlapping values are fully masked
    return sorted(set(values), key=len, reverse=True)


def redact(text: str | None, environ: Mapping[str, str] | None = None) -> Redacted:
    if not text:
        return Redacted(text or "", 0, ())
    env = os.environ if environ is None else environ
    count = 0
    hit: list[str] = []
    for value in _secret_env_values(env):
        if value in text:
            n = text.count(value)
            text = text.replace(value, MASK)
            count += n
            hit.append("env_value")
    for name, pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        if n:
            count += n
            hit.append(name)
    return Redacted(text, count, tuple(dict.fromkeys(hit)))


def redact_obj(obj: Any, environ: Mapping[str, str] | None = None) -> tuple[Any, int]:
    """Recursively redact strings inside JSON-like data. Secret-named keys are masked whole."""
    if isinstance(obj, str):
        r = redact(obj, environ)
        return r.text, r.count
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        total = 0
        for k, v in obj.items():
            if isinstance(v, str) and _SECRET_NAME_RE.match(str(k)) and v:
                out[k] = MASK
                total += 1
                continue
            out[k], n = redact_obj(v, environ)
            total += n
        return out, total
    if isinstance(obj, list | tuple):
        items = []
        total = 0
        for v in obj:
            rv, n = redact_obj(v, environ)
            items.append(rv)
            total += n
        return items, total
    return obj, 0


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(limit - 40, 10)
    head = keep * 2 // 3
    tail = keep - head
    return f"{text[:head]} …[{len(text) - keep} chars omitted]… {text[-tail:]}"


def sanitize(text: str | None, limit: int, environ: Mapping[str, str] | None = None) -> Redacted:
    """Redact then bound. Redaction runs on the full text so truncation can't split a secret."""
    r = redact(text, environ)
    return Redacted(truncate_middle(r.text, limit), r.count, r.rules)


def keyed_hash(value: str, salt: bytes) -> str:
    """Stable, non-reversible identifier for sensitive raw values (e.g. absolute paths)."""
    return hmac.new(salt, value.encode(), hashlib.sha256).hexdigest()[:24]


def contains_secret(text: str, environ: Mapping[str, str] | None = None) -> bool:
    return redact(text, environ).count > 0
