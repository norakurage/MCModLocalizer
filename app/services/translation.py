"""Translation workflow orchestration."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


from ..core import SYSTEM_INSTRUCTIONS_BASE, protect_tokens, restore_tokens
from ..core.chunking import chunk_pairs
from ..core.json_io import load_json, write_json
from ..core.translation_batch import translate_batch
from ..core.usage import UsageStats
from .resource_pack import collect_pack_translations

LogFn = Optional[Callable[[str], None]]
ProgressFn = Optional[Callable[[float, str], None]]
StopFn = Optional[Callable[[], bool]]
BuildPackFn = Optional[Callable[[List[Tuple[str, Path]]], Optional[Path]]]


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


@dataclass
class TranslationSummary:
    translated_mods: int
    total_mods: int
    translated_entries: int
    total_entries: int
    aborted: bool
    had_error: bool
    pack_dir: Optional[Path]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    usage_records: List[Tuple[int, int, int]] = field(default_factory=list)


def parse_fraction(text: str) -> Optional[Tuple[int, int]]:
    """'3/10' のような文字列を (3, 10) に解釈する。失敗時は None。"""
    if "/" not in text:
        return None
    left, right = text.split("/", 1)
    try:
        return int(left.strip()), int(right.strip())
    except ValueError:
        return None


def run_translation_jobs(
    targets: List[Tuple[str, Path, Dict[str, str]]],
    *,
    api_key: str,
    model: str,
    output_dir: Path,
    build_pack: BuildPackFn = None,
    log: LogFn = None,
    set_overall: ProgressFn = None,
    set_detail: ProgressFn = None,
    should_stop: StopFn = None,
) -> TranslationSummary:
    """複数 Mod の翻訳を順に実行し、逐次リソースパックを更新する。

    UI 非依存。進捗・ログ・停止判定・パック生成はすべて callback として注入する。
    stop_event やボタン状態など UI のライフサイクル管理は呼び出し側の責務。
    """
    _log = log or (lambda *_a: None)
    _overall = set_overall or (lambda *_a: None)
    _detail = set_detail or (lambda *_a: None)
    if should_stop is None:
        should_stop = lambda: False
    if build_pack is None:
        build_pack = lambda _produced: None

    total_targets = len(targets)
    produced: List[Tuple[str, Path]] = []
    aborted = False
    had_error = False
    total_entries = 0
    translated_entries = 0
    pack_dir_path: Optional[Path] = None
    pack_generated_once = False
    _overall(0.0, "翻訳準備中")
    _detail(0.0, "")
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_token_count = 0
    usage_records: List[Tuple[int, int, int]] = []
    existing_pack_translations = collect_pack_translations(output_dir)
    resume_root = output_dir / ".resume"
    skipped_existing = 0

    def _register_pack_contents(pack_root: Optional[Path]) -> None:
        if not pack_root:
            return
        for mod_name, lang_path in collect_pack_translations(pack_root).items():
            existing_pack_translations[mod_name] = lang_path

    for idx, (modid, in_path, existing_ja) in enumerate(targets, start=1):
        if should_stop():
            aborted = True
            break
        if not in_path.exists():
            _log(f"[WARN] en_us.json が見つかりません: {modid}")
            continue
        out_path = in_path.parent / "ja_jp.json"
        completed_mods = idx - 1
        overall_ratio = completed_mods / total_targets if total_targets else 0.0
        _overall(overall_ratio, f"翻訳中: {completed_mods}件完了（全{total_targets}件）")
        _log(f"[RUN] 翻訳を開始します: {modid}")
        _detail(0.0, f"{modid}: 0%")

        def _progress_wrapper(ratio: float, text: str, *, _modid: str = modid) -> None:
            parsed = parse_fraction(text.strip())
            if parsed:
                done, total = parsed
                label = f"{_modid}: {done}件完了（全{total}件）"
            else:
                percent = int(max(0.0, min(1.0, ratio)) * 100)
                label = f"{_modid}: {percent}%"
            _detail(ratio, label)

        pack_lang_path = existing_pack_translations.get(modid)
        resume_path = resume_root / modid / "ja_jp.json"
        resume_exists = resume_path.exists()
        if resume_exists:
            _log(f"[INFO] 中断済みの翻訳ファイルを検出しました（{modid}）。未訳を引き継ぎます。")
        if (
            pack_lang_path
            and pack_lang_path.exists()
            and resume_root not in pack_lang_path.parents
        ):
            skipped_existing += 1
            _log(f"[INFO] mods_ja_resource に既存の翻訳が見つかったためスキップします（{modid}）。")
            overall_ratio = idx / total_targets if total_targets else 1.0
            _overall(overall_ratio, f"翻訳中: {idx}件完了（全{total_targets}件）")
            _detail(1.0, f"{modid}: 既存訳を使用")
            continue

        try:
            result = translate_localizations(
                api_key=api_key,
                model=model,
                in_path=in_path,
                out_path=out_path,
                existing_translations=existing_ja,
                log=_log,
                progress=_progress_wrapper,
                should_stop=should_stop,
                resume_path=resume_path,
            )
            total_entries += result.total
            translated_entries += result.created
            total_prompt_tokens += result.prompt_tokens
            total_completion_tokens += result.completion_tokens
            total_token_count += result.total_tokens
            for usage in result.usages:
                prompt = usage.prompt_tokens
                completion = usage.completion_tokens
                total_tok = usage.total_tokens or (prompt + completion)
                usage_records.append((prompt, completion, total_tok))
            remaining = max(0, result.total - result.created)
            if result.stopped:
                if remaining:
                    _log(f"[INFO] 未翻訳 {remaining} 件の進捗を保存しました。再開時は自動的に続きから処理します。")
                elif resume_exists:
                    _log("[INFO] 停止時点の翻訳は保存済みです。再開時に利用されます。")
                _log("[INFO] ユーザーによって翻訳が停止されました。")
                aborted = True
                break
            if out_path.exists():
                produced.append((modid, out_path))
            _log(f"[OK] ja_jp.json を作成しました（{modid}）。")
            pack_dir = build_pack(produced)
            if pack_dir:
                pack_dir_path = pack_dir
                pack_generated_once = True
                _log(f"[OK] リソースパックを更新しました（{modid}）。")
                _register_pack_contents(pack_dir)
            if not aborted:
                overall_ratio = idx / total_targets if total_targets else 1.0
                _overall(overall_ratio, f"翻訳中: {idx}件完了（全{total_targets}件）")
        except Exception as ex:
            had_error = True
            _log(f"[ERROR] 翻訳処理で例外 ({modid}): {repr(ex)}")
            _log(traceback.format_exc())
            aborted = True
            break

    if produced and not aborted and not pack_generated_once:
        try:
            pack_dir = build_pack(produced)
            if pack_dir:
                pack_dir_path = pack_dir
                _log(f"[OK] リソースパックを更新しました（{pack_dir.name}）。")
                pack_png = pack_dir / "pack.png"
                if not pack_png.exists():
                    _log(f"[INFO] pack.png は手動で配置してください（{pack_dir.name}）。")
                _register_pack_contents(pack_dir)
        except Exception as ex:
            had_error = True
            _log(f"[ERROR] リソースパックの生成に失敗しました: {repr(ex)}")
            _log(traceback.format_exc())
    elif aborted:
        _log("[WARN] 翻訳が完了しなかったため、リソースパックの作成をスキップしました。")
    elif skipped_existing:
        _log(f"[INFO] {skipped_existing} 件の Mod は mods_ja_resource に既存の翻訳があったため処理をスキップしました。")
    else:
        _log("[WARN] ja_jp.json が生成されなかったため、リソースパックの作成をスキップしました。")

    return TranslationSummary(
        translated_mods=len(produced),
        total_mods=total_targets,
        translated_entries=translated_entries,
        total_entries=total_entries,
        aborted=aborted,
        had_error=had_error,
        pack_dir=pack_dir_path,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        total_tokens=total_token_count,
        model=model,
        usage_records=usage_records,
    )


__all__ = [
    "TranslationResult",
    "TranslationSummary",
    "translate_localizations",
    "run_translation_jobs",
    "parse_fraction",
]
