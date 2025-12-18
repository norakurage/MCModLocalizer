"""Translation workflow orchestration."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


from ..core import SYSTEM_INSTRUCTIONS_BASE, protect_tokens, restore_tokens
from ..core.chunking import chunk_pairs
from ..core.json_io import load_json, write_json
from ..core.translation_batch import translate_batch
from ..core.usage import UsageStats

LogFn = Optional[Callable[[str], None]]
ProgressFn = Optional[Callable[[float, str], None]]
StopFn = Optional[Callable[[], bool]]


@dataclass
class TranslationResult:
    total: int
    created: int
    out_path: Path
    stopped: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usages: List[UsageStats] = field(default_factory=list)


def translate_localizations(
    api_key: str,
    model: str,
    in_path: Path,
    out_path: Path,
    existing_translations: Optional[Dict[str, str]] = None,
    *,
    log: LogFn = None,
    progress: ProgressFn = None,
    should_stop: StopFn = None,
    sleep_interval: float = 0.4,
    resume_path: Optional[Path] = None,
) -> TranslationResult:
    if should_stop is None:
        should_stop = lambda: False
    if log:
        log("[RUN] 入力ファイルを読み込みます。")
        log("[RUN] 出力ファイルを準備します。")
    src: Dict[str, str] = load_json(in_path)
    dst: Dict[str, str] = load_json(out_path)
    resume_data: Dict[str, str] = {}

    def _merge_missing(source: Dict[str, str]) -> bool:
        if not source:
            return False
        nonlocal dst
        if not isinstance(dst, dict):
            dst = {}
        merged = False
        for key, value in source.items():
            if str(dst.get(key, "")).strip() == "" and str(value).strip() != "":
                dst[key] = str(value)
                merged = True
        return merged

    if existing_translations:
        if not dst:
            dst = dict(existing_translations)
            if log:
                log("[INFO] 既存の ja_jp.json が見つかったため差分のみを補完します。")
        else:
            if log:
                log("[INFO] 既存の ja_jp.json を差分チェックに利用します。")
            _merge_missing(existing_translations)
    if resume_path and resume_path.exists():
        resume_data = load_json(resume_path)
        if log and resume_data:
            log("[INFO] 中断された翻訳データを読み込みます。")
        if _merge_missing(resume_data) and log:
            log("[INFO] 中断データから未訳を引き継ぎます。")
    todo: List[Tuple[str, str]] = []
    base_token_maps: Dict[str, Dict[str, str]] = {}
    for k, v in src.items():
        sv = str(v)
        if k in dst and str(dst[k]).strip() != "":
            continue
        pv, base_map = protect_tokens(sv)
        if base_map:
            base_token_maps[k] = base_map
        todo.append((k, pv))
    if existing_translations and log:
        log(
            f"[INFO] 既存訳 {len(existing_translations)} 件を検出。未訳 {len(todo)} 件を補完します。"
        )
    batches = list(chunk_pairs(todo))
    total = sum(len(batch) for batch in batches)
    if total == 0:
        if log:
            log("[OK] すでに翻訳済みです（差分なし）。")
        if progress:
            progress(1.0, "完了")
        if resume_path and resume_path.exists():
            try:
                resume_path.unlink()
                parent = resume_path.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                pass
        return TranslationResult(total=0, created=0, out_path=out_path, stopped=False)
    system_instructions = SYSTEM_INSTRUCTIONS_BASE
    


    created = 0
    stopped = False
    usage_total = UsageStats()
    usage_batches: List[UsageStats] = []
    if progress and total:
        progress(0.0, f"0/{total}")
    for batch_index, batch in enumerate(batches, start=1):
        if should_stop():
            stopped = True
            if log:
                log("[STOP] ユーザーによって停止されました。現在までの結果を保存します。")
            break
        kv: Dict[str, Tuple[str, Dict[str, str]]] = {}
        payload: List[Dict[str, str]] = []
        for k, protected in batch:
            kv[k] = (protected, {})
            payload.append({"key": k, "value": protected})
        out_map, batch_usage = translate_batch(
            api_key,
            payload,
            model=model,
            system_instructions=system_instructions,
            log_fn=log,
        )
        usage_total.add(batch_usage)
        usage_batches.append(batch_usage)
        for k, (protected2, m) in kv.items():
            ja = out_map.get(k, "") or protected2
            ja = restore_tokens(ja, m)
            base_map = base_token_maps.get(k)
            if base_map:
                ja = restore_tokens(ja, base_map)
            dst[k] = ja
            created += 1
        if progress:
            ratio = created / max(1, total)
            progress(ratio, f"{created}/{total}")
        if log:
            log(f"[INFO] バッチ完了: {created}件（全{total}件）")
        if sleep_interval > 0:
            time.sleep(sleep_interval)
    write_json(out_path, dst)
    if log:
        log("[OK] 書き込み完了。")
    if progress:
        final_ratio = created / max(1, total)
        progress(1.0 if not stopped else final_ratio, f"{created}/{total}")
    remaining = sum(1 for k in src if str(dst.get(k, "")).strip() == "")
    if resume_path:
        if remaining > 0 or stopped:
            write_json(resume_path, dst)
            if log:
                log("[INFO] 翻訳の進捗を保存しました。")
        else:
            try:
                if resume_path.exists():
                    resume_path.unlink()
                parent = resume_path.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                pass
    return TranslationResult(
        total=total,
        created=created,
        out_path=out_path,
        stopped=stopped,
        prompt_tokens=usage_total.prompt_tokens,
        completion_tokens=usage_total.completion_tokens,
        total_tokens=usage_total.total_tokens,
        usages=usage_batches,
    )


__all__ = ["TranslationResult", "translate_localizations"]
