"""Utility helpers for splitting translation payloads."""
from __future__ import annotations

import json
from typing import Iterable, List, Tuple


def chunk_pairs(
    pairs: Iterable[Tuple[str, str]],
    max_chars: int = 6000,
    max_items: int = 80,
) -> Iterable[List[Tuple[str, str]]]:
    buf: List[Tuple[str, str]] = []
    chars = 0
    for k, v in pairs:
        item_json = json.dumps({"key": k, "value": v}, ensure_ascii=False)
        if (len(buf) >= max_items) or (chars + len(item_json) > max_chars):
            yield buf
            buf = []
            chars = 0
        buf.append((k, v))
        chars += len(item_json)
    if buf:
        yield buf


__all__ = ["chunk_pairs"]
