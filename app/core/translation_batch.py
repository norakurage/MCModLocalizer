"""Translation batch execution helpers."""
from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

from openai import OpenAI

from .constants import USER_TEMPLATE
from .usage import UsageStats, usage_from_response


def translate_batch(
    client: OpenAI,
    items: List[Dict[str, str]],
    model: str,
    system_instructions: str,
    _retry_depth: int = 0,
) -> Tuple[Dict[str, str], UsageStats]:
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    user_text = USER_TEMPLATE.replace("<<PAYLOAD>>", payload)
    expected_keys = [it["key"] for it in items]
    unique_keys = list(dict.fromkeys(expected_keys))
    if not expected_keys:
        return {}, UsageStats()
    expected_len = len(expected_keys)
    response_format_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "translation_list",
            "schema": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": expected_len,
                "maxItems": expected_len,
            },
        },
    }
    last_raw: str = ""

    def _parse_list(out: str) -> List[str]:
        try:
            obj = json.loads(out)
            if isinstance(obj, list):
                return [str(v) for v in obj]
            if isinstance(obj, dict):
                ordered: List[str] = []
                for key in unique_keys:
                    if key in obj:
                        ordered.append(str(obj[key]))
                if ordered:
                    return ordered
        except Exception:
            pass
        m = re.search(r"\[.*\]", out or "", re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, list):
                    return [str(v) for v in obj]
            except Exception:
                pass
        if out:
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            if len(lines) >= expected_len:
                return lines[:expected_len]
        return []

    def _call_responses(with_response_format: bool, extra_note: str = "") -> Tuple[List[str], UsageStats]:
        nonlocal last_raw
        args = dict(
            model=model,
            instructions=system_instructions + extra_note,
            input=user_text,
        )
        if with_response_format:
            args["response_format"] = response_format_schema
        try:
            resp = client.responses.create(**args)  # type: ignore[arg-type]
        except TypeError:
            if with_response_format:
                return _call_responses(
                    False,
                    extra_note
                    + "\n出力は必ず『単一の JSON 配列（順番どおりの日本語訳）』のみで返してください。",
                )
            raise
        usage = usage_from_response(resp)
        out = getattr(resp, "output_text", None)
        if out:
            last_raw = out
            return _parse_list(out), usage
        out_parts: List[str] = []
        output = getattr(resp, "output", None)
        if output:
            for seg in output:
                content = getattr(seg, "content", None)
                if content:
                    for c in content:
                        text = getattr(c, "text", None)
                        if text:
                            out_parts.append(text)
                        else:
                            j = getattr(c, "json", None)
                            if j is not None:
                                out_parts.append(json.dumps(j, ensure_ascii=False))
        last_raw = "".join(out_parts)
        return _parse_list(last_raw), usage

    def _call_chat(extra_note: str = "") -> Tuple[List[str], UsageStats]:
        nonlocal last_raw
        messages = [
            {"role": "system", "content": system_instructions + extra_note},
            {"role": "user", "content": user_text},
        ]
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format=response_format_schema,
        )
        usage = usage_from_response(resp)
        content = ""
        if getattr(resp, "choices", None):
            msg = resp.choices[0].message
            content = getattr(msg, "content", None) or ""
        last_raw = content or ""
        return _parse_list(content or ""), usage

    data_list, usage = _call_responses(True)
    if len(data_list) < expected_len:
        note = (
            "\n出力は次の形式のみ：[<訳1>, <訳2>, ...]（items と同じ順序・要素数）。余計な文字や説明は一切書かないこと。"
        )
        chat_list, chat_usage = _call_chat(note)
        usage.add(chat_usage)
        data_list = chat_list
    if len(data_list) < expected_len:
        missing_count = expected_len - len(data_list)
        if missing_count and _retry_depth < 2:
            start_index = len(data_list)
            subset_items: List[Dict[str, str]] = items[start_index:]
            if subset_items:
                subset_map, subset_usage = translate_batch(
                    client,
                    subset_items,
                    model,
                    system_instructions,
                    _retry_depth=_retry_depth + 1,
                )
                usage.add(subset_usage)
                for idx in range(start_index, expected_len):
                    key = expected_keys[idx]
                    val = subset_map.get(key, "")
                    if idx < len(data_list):
                        data_list[idx] = val
                    else:
                        data_list.append(val)
    if len(data_list) < expected_len:
        snippet = (last_raw or "").strip().replace("\r", " ").replace("\n", " ")[:400]
        raise RuntimeError(
            f"LLM output returned {len(data_list)}/{expected_len} translations. Raw snippet: {snippet}"
        )
    if len(data_list) > expected_len:
        data_list = data_list[:expected_len]
    ordered: Dict[str, str] = {}
    for idx, key in enumerate(expected_keys):
        value = data_list[idx] if idx < len(data_list) else ""
        ordered[str(key)] = str(value)
    return ordered, usage


__all__ = ["translate_batch"]
