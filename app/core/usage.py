"""Usage tracking utilities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class UsageStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "UsageStats") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


def _coerce_int(value: object) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip():
            return int(float(value.strip()))
    except Exception:
        return 0
    return 0


def usage_from_response(resp) -> UsageStats:  # type: ignore[no-untyped-def]
    if isinstance(resp, dict):
        usage = resp.get("usage")
    else:
        usage = getattr(resp, "usage", None)

    if usage is None:
        return UsageStats()

    if hasattr(usage, "to_dict"):
        try:
            usage = usage.to_dict()
        except Exception:
            pass

    data: Dict[str, object]
    if isinstance(usage, dict):
        data = usage
    else:
        data = getattr(usage, "__dict__", {})  # type: ignore[assignment]

    def _extract(*keys: str) -> int:
        for key in keys:
            if isinstance(data, dict) and key in data:
                return _coerce_int(data[key])
            if hasattr(usage, key):
                return _coerce_int(getattr(usage, key))
        return 0

    prompt = _extract("prompt_tokens", "input_tokens")
    completion = _extract("completion_tokens", "output_tokens")
    total = _extract("total_tokens")
    if total == 0 and (prompt or completion):
        total = prompt + completion
    return UsageStats(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


__all__ = ["UsageStats", "usage_from_response"]
