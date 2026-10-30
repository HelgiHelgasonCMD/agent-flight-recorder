"""Tokenizer tuned for engineering text.

Emits both the *compound* token (``err_require_esm``, ``--no-cache-dir``,
``src/app/config.py``, ``numpy==2.0.1``) and its sub-parts (``err``, ``require``, ``esm``),
so exact error codes, CLI flags, paths and versions match exactly while partial
mentions still match their parts. CamelCase identifiers are split as well
(``ModuleNotFoundError`` → ``modulenotfounderror``, ``module``, ``not``, ``found``, ``error``).
"""

from __future__ import annotations

import re

_COMPOUND = re.compile(r"[^\s\"'`()\[\]{}<>,;]+")
_STRIP = ".:!?=+*#|\\"
_PARTS = re.compile(r"[A-Za-z]+|\d+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "not",
        "no",
        "can",
        "could",
        "should",
        "would",
        "do",
        "does",
        "did",
        "you",
        "your",
        "we",
        "our",
        "they",
        "them",
        "he",
        "she",
        "his",
        "her",
        "after",
        "before",
        "when",
        "while",
        "what",
        "which",
        "who",
        "how",
        "why",
    ]
)
# "not" carries meaning in error text ("No module named", "not found"): keep it when part of a compound
_KEEP_IN_COMPOUND = frozenset({"not", "no"})


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _COMPOUND.finditer(text):
        raw = m.group(0).strip(_STRIP)
        if not raw:
            continue
        low = raw.lower()
        parts: list[str] = []
        for chunk in _PARTS.findall(raw):
            parts.extend(p.lower() for p in _CAMEL.findall(chunk))
        is_compound = len(parts) > 1 or any(c in raw for c in "._-/=@:")
        if is_compound and len(low) <= 80:
            out.append(low)
        for p in parts:
            if p in STOPWORDS and not (is_compound and p in _KEEP_IN_COMPOUND):
                continue
            if len(p) == 1 and not p.isdigit():
                continue
            if p != low or not is_compound:
                out.append(p)
    return out
