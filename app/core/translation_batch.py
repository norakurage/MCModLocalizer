"""Translation batch execution helpers."""
from __future__ import annotations

import json
import re
import time
from typing import Callable, Dict, List, Optional, Tuple



from .constants import USER_TEMPLATE
from .usage import UsageStats, usage_from_response


def translate_batch(
    api_key: str,
    items: List[Dict[str, str]],
    model: str,
    system_instructions: str,
    log_fn: Optional[Callable[[str], None]] = None,
    _retry_depth: int = 0,
) -> Tuple[Dict[str, str], UsageStats]:
    values = [item["value"] for item in items]
    payload = json.dumps(values, ensure_ascii=False, indent=2)
    user_text = USER_TEMPLATE.replace("<<PAYLOAD>>", payload)
    expected_keys = [it["key"] for it in items]
    unique_keys = list(dict.fromkeys(expected_keys))
    if not expected_keys:
        return {}, UsageStats()
    expected_len = len(expected_keys)
    response_format_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "translation_result",
            "schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
    last_raw: str = ""

    def _parse_list(out: str) -> List[str]:
        try:
            obj = json.loads(out)
            # New schema format: {"items": [...]}
            if isinstance(obj, dict) and "items" in obj and isinstance(obj["items"], list):
                return [str(v) for v in obj["items"]]
            
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

    import urllib.request
    import urllib.error

    def _call_chat(extra_note: str = "") -> Tuple[List[str], UsageStats]:
        nonlocal last_raw
        messages = [
            {"role": "system", "content": system_instructions + extra_note},
            {"role": "user", "content": user_text},
        ]
        
        request_body = {
            "model": model,
            "messages": messages,
            "response_format": response_format_schema,
        }

        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        


        max_retries = 5
        retry_delay = 5.0

        for attempt in range(max_retries):
            try:
                print(f"--- [DEBUG] SEND User (Attempt {attempt+1}/{max_retries}) ---\n{user_text}\n-------------------------")
                
                data_bytes = json.dumps(request_body).encode("utf-8")
                req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
                
                with urllib.request.urlopen(req) as response:
                    resp_body = response.read().decode("utf-8")
                    resp = json.loads(resp_body)
                
                # Convert dict to usage object or extracting manually
                # current usage_from_response handles dict inputs
                usage = usage_from_response(resp)
                
                content = ""
                if "choices" in resp and len(resp["choices"]) > 0:
                    content = resp["choices"][0].get("message", {}).get("content", "") or ""
                    print(f"--- [DEBUG] RECV Assistant ---\n{content}\n-----------------------------")
                
                last_raw = content or ""
                return _parse_list(content or ""), usage

            except urllib.error.HTTPError as e:
                # Immediate failure conditions
                if e.code in (400, 401, 403):
                    print(f"--- [ERROR] Immediate failure HTTP {e.code}: {e.reason}")
                    raise

                # Retry conditions (429, 500, 502, 503) or others
                # Though we specifically target 429/5xx, general retry for others is safer unless specified otherwise.
                # But here we focus on the requirement.
                
                # Check if it's a retryable error
                is_retryable = e.code in (429, 500, 502, 503)
                
                if not is_retryable:
                    # If it's not in the explicit retry list AND not in the immediate fail list,
                    # we have to decide. Given the user requirement implies a split, 
                    # let's assume anything else is also immediate failure or maybe just fail to be safe?
                    # "400 / 401 / 403 -> Immediate"
                    # "429 / 500 / 502 / 503 -> Retry"
                    # Default: Let's treat valid 4xx (client error) as immediate fail if not 429?
                    # But to be safe and robust, usually we only retry transient errors.
                    # 404? 405? -> fail.
                    pass 
                    
                # If we are here, we are either 429, 5xx, or decided to retry?
                # Actually, let's strictly follow the user's implicit "Retry these, Fail those" logic.
                # If it's 429 or 5xx, we retry.
                
                should_retry = e.code in (429, 500, 502, 503)
                
                if not should_retry:
                    # Fallback for unhandled codes -> Raise
                     print(f"--- [ERROR] Unhandled HTTP {e.code}: {e.reason}")
                     raise

                # Retry logic
                if attempt == max_retries - 1:
                    msg = f"[ERROR] Retry limit exceeded for HTTP {e.code}."
                    print(f"--- {msg}")
                    # raise the original error
                    raise

                wait_time = retry_delay
                actual_wait = max(wait_time, retry_delay)
                msg = f"[WARN] HTTP {e.code} ({e.reason}). Waiting {actual_wait:.2f}s... (Attempt {attempt+1}/{max_retries})"
                print(f"--- {msg}")
                if log_fn:
                    log_fn(msg)
                time.sleep(actual_wait)
                
                retry_delay *= 2
            
            except Exception as e:
                # Network errors etc.
                if attempt == max_retries - 1:
                    raise
                msg = f"[WARN] Error: {e}. Retrying in {retry_delay}s..."
                print(f"--- {msg}")
                if log_fn:
                    log_fn(msg)
                time.sleep(retry_delay)
                retry_delay *= 2

        # Should not maximize here due to raises
        return [], UsageStats()

    data_list, usage = _call_chat()
    if len(data_list) < expected_len:
        note = (
            "\n出力は次の形式のみ：{\"items\": [<訳1>, <訳2>, ...]}（items と同じ順序・要素数）。余計な文字や説明は一切書かないこと。"
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
                    api_key,
                    subset_items,
                    model,
                    system_instructions,
                    log_fn=log_fn,
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
