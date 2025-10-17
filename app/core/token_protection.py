"""Helpers for protecting and restoring placeholder tokens."""
from __future__ import annotations

from typing import Dict, Tuple

from .constants import PROTECT_RE


def protect_tokens(s: str) -> Tuple[str, Dict[str, str]]:
    mapping: Dict[str, str] = {}
    idx = 0

    def repl(m) -> str:  # type: ignore[no-untyped-def]
        nonlocal idx
        token = m.group(0)
        key = f"‹T{idx}›"
        mapping[key] = token
        idx += 1
        return key

    protected = PROTECT_RE.sub(repl, s)
    return protected, mapping


def restore_tokens(s: str, mapping: Dict[str, str]) -> str:
    for k, v in mapping.items():
        s = s.replace(k, v)
    return s


__all__ = ["protect_tokens", "restore_tokens"]
