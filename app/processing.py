from __future__ import annotations

import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from openai import OpenAI

# ------------------------------
# トークン保護（翻訳で壊されたくないもの）
# ------------------------------
PLACEHOLDER_PATTERNS = [
    r"%\d+\$[sd]",       # %1$s, %2$d
    r"%[sd]",            # %s, %d
    r"\{[a-zA-Z0-9_]+\}",# {name}
    r"\{\d+\}",          # {0}
]
COLOR_CODES = [r"§[0-9a-fk-or]"]
ESCAPES = [r"\\n", r"\\t", r"\\r"]
PROTECT_RE = re.compile("|".join(PLACEHOLDER_PATTERNS + COLOR_CODES + ESCAPES))

SYSTEM_INSTRUCTIONS_BASE = """あなたは熟練のローカライザーです。出力は必ず日本語で、自然で簡潔に訳してください。
Minecraft の Mod 用テキスト（ゲーム内のUI/メッセージ/アイテム名）です。次を厳守：
- 与えられたキーは変更しない（値のみ翻訳）
- ‹T0› のような保護トークンは絶対に改変・和訳しない（位置もできるだけ原文通り）
- 句読点・全角/半角の不自然さを避ける。文末の余分な空白を付けない
- 固有名詞/アイテムID/コマンドは文脈上そのまま残す（例: “Minecraft”, “Redstone”, “/reload”）
- バニラ Minecraft の公式日本語名が既に存在する語は尊重し、勝手に別訳へ置き換えない
- 技術語は日本のマイクラ文脈で一般的な用語に統一（例: “Stack”→“スタック”、ただし固有名は維持）
- 改行や \\n は原文通り保持
- 返答は必ず JSON 配列（各要素が翻訳テキスト）で返す
"""

USER_TEMPLATE = """以下の items は { \"key\":..., \"value\":... } の配列です。
出力は **単一の JSON 配列のみ** とし、構造は次の通りです。
- 配列の要素数は items と同じにする
- 配列の i 番目の要素は items[i].value の日本語訳とする（保護トークン ‹Tn› は原文どおりそのまま残す）
【入力例】
items:
[
  {\"key\":\"block.example.copper_block\",\"value\":\"Copper Block\"},
  {\"key\":\"message.example.tips\",\"value\":\"Press ‹T0› to open the menu.\"}
]
【出力例】（この形式以外は出力しない）
[
  \"銅のブロック\",
  \"メニューを開くには ‹T0› を押します。\"
]
items:
<<PAYLOAD>>
"""

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]
StopFn = Callable[[], bool]


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


def _usage_from_response(resp) -> UsageStats:
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


def protect_tokens(s: str) -> Tuple[str, Dict[str, str]]:
    mapping: Dict[str, str] = {}
    idx = 0

    def repl(m: re.Match) -> str:
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


def chunk_pairs(pairs: List[Tuple[str, str]], max_chars: int = 6000, max_items: int = 80):
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

    def _extract_text(resp) -> str:
        txt = getattr(resp, "output_text", None)
        if txt:
            return txt
        out_parts: List[str] = []
        output = getattr(resp, "output", None)
        if output:
            for seg in output:
                content = getattr(seg, "content", None)
                if content:
                    for c in content:
                        t = getattr(c, "text", None)
                        if t:
                            out_parts.append(t)
                        else:
                            j = getattr(c, "json", None)
                            if j is not None:
                                out_parts.append(json.dumps(j, ensure_ascii=False))
        return "".join(out_parts)

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
        streamed_parts: List[str] = []
        try:
            with client.responses.stream(**args) as stream:  # type: ignore[arg-type]
                for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            streamed_parts.append(str(delta))
                    elif event_type == "response.error":
                        error = getattr(event, "error", None)
                        message = getattr(error, "message", None) if error else None
                        raise RuntimeError(message or "OpenAI streaming error")
                resp = stream.get_final_response()
        except TypeError:
            if with_response_format:
                return _call_responses(
                    False,
                    extra_note + "\n出力は必ず『単一の JSON 配列（順番どおりの日本語訳）』のみで返してください。"
                )
            raise
        usage = _usage_from_response(resp)
        out = _extract_text(resp)
        if not out and streamed_parts:
            out = "".join(streamed_parts)
        last_raw = out or ""
        return _parse_list(out), usage

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
        usage = _usage_from_response(resp)
        content = ""
        if getattr(resp, "choices", None):
            msg = resp.choices[0].message
            content = getattr(msg, "content", None) or ""
        last_raw = content or ""
        return _parse_list(content or ""), usage

    data_list, usage = _call_responses(True)
    if len(data_list) < expected_len:
        note = ("\n出力は次の形式のみ：[<訳1>, <訳2>, ...]（items と同じ順序・要素数）。余計な文字や説明は一切書かないこと。")
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


def load_json(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


MOD_LANG_PATTERN = re.compile(r"^assets/([^/]+)/lang/([a-z0-9_\-]+)\.json$", re.IGNORECASE)


def read_lang_from_jar(jar_path: Path, locale: str) -> Dict[str, Dict[str, str]]:
    """JAR 内の assets/<modid>/lang/<locale>.json を全て読み取る。"""
    target_locale = locale.lower()
    out: Dict[str, Dict[str, str]] = {}
    with zipfile.ZipFile(jar_path, "r") as zf:
        for name in zf.namelist():
            m = MOD_LANG_PATTERN.match(name)
            if not m:
                continue
            modid, lang = m.group(1), m.group(2).lower()
            if lang != target_locale:
                continue
            try:
                with zf.open(name) as f:
                    data = json.loads(f.read().decode("utf-8"))
                out[modid] = {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass
    return out


def read_en_us_from_jar(jar_path: Path) -> Dict[str, Dict[str, str]]:
    return read_lang_from_jar(jar_path, "en_us")


def choose_primary_modid(mod_maps: Dict[str, Dict[str, str]]) -> Tuple[str, Dict[str, str]]:
    if not mod_maps:
        raise ValueError("JAR 内に en_us.json が見つかりません。")
    items = sorted(mod_maps.items(), key=lambda kv: len(kv[1]), reverse=True)
    return items[0][0], items[0][1]


def write_json(path: Path, data: Dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class ExtractionResult:
    primary_modid: Optional[str]
    primary_en_path: Optional[Path]
    mod_maps: Dict[str, Dict[str, str]]
    mod_sources: Dict[str, Path] = field(default_factory=dict)
    existing_ja_maps: Dict[str, Dict[str, str]] = field(default_factory=dict)


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


def extract_localizations(
    source_path: Path,
    out_dir: Path,
    *,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
) -> ExtractionResult:
    if source_path.is_dir():
        jar_paths = sorted(p for p in source_path.iterdir() if p.is_file() and p.suffix.lower() == ".jar")
        if not jar_paths:
            raise ValueError("指定されたフォルダ内に Mod の JAR が見つかりませんでした。")
    else:
        jar_paths = [source_path]

    aggregated_maps: Dict[str, Dict[str, str]] = {}
    existing_lang_maps: Dict[str, Dict[str, str]] = {}
    mod_sources: Dict[str, Path] = {}
    primary_modid: Optional[str] = None
    primary_en_path: Optional[Path] = None
    primary_map: Optional[Dict[str, str]] = None

    total_mods = 0
    per_jar_maps: List[Tuple[Path, Dict[str, Dict[str, str]]]] = []
    for jar in jar_paths:
        mod_maps = read_en_us_from_jar(jar)
        if not mod_maps:
            if log:
                log(f"[WARN] en_us.json が見つからないためスキップしました: {jar.name}")
            continue
        per_jar_maps.append((jar, mod_maps))
        total_mods += len(mod_maps)

    if not per_jar_maps:
        raise ValueError("Mod の en_us.json が 1 件も見つかりませんでした。")

    done = 0
    for jar, mod_maps in per_jar_maps:
        if len(mod_maps) > 1 and log:
            mods_lines = "\n".join(
                f"  - {m} ({len(d)} キー)" for m, d in mod_maps.items()
            )
            log(
                f"[WARN] 複数 namespace を含む Mod を検出しました ({jar.name}):\n{mods_lines}\n全て処理します。"
            )
        ja_maps = read_lang_from_jar(jar, "ja_jp")
        for modid, en_map in mod_maps.items():
            mod_dir = out_dir / modid
            en_path = mod_dir / "en_us.json"
            write_json(en_path, en_map)
            existing_ja = ja_maps.get(modid)
            if existing_ja:
                write_json(mod_dir / "ja_jp.json", existing_ja)
                existing_lang_maps[modid] = existing_ja
            aggregated_maps[modid] = en_map
            mod_sources[modid] = jar
            if log:
                note = " (既存の ja_jp を読み込み)" if existing_ja else ""
                log(f"[OK] 抽出: {modid}{note}")
            if primary_modid is None or (len(en_map) > len(primary_map or {})):
                primary_modid = modid
                primary_map = en_map
                primary_en_path = en_path
            done += 1
            if progress and total_mods:
                progress(done / total_mods, f"{done}/{total_mods}")

    if log and primary_modid and primary_map is not None:
        log(f"[INFO] 最大キー数の Mod: {primary_modid}（キー数: {len(primary_map)}）")
    if progress and total_mods:
        progress(1.0, f"{total_mods}/{total_mods}")

    return ExtractionResult(
        primary_modid=primary_modid,
        primary_en_path=primary_en_path,
        mod_maps=aggregated_maps,
        mod_sources=mod_sources,
        existing_ja_maps=existing_lang_maps,
    )


def translate_localizations(
    api_key: str,
    model: str,
    in_path: Path,
    out_path: Path,
    existing_translations: Optional[Dict[str, str]] = None,
    *,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[StopFn] = None,
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
    client = OpenAI(api_key=api_key)
    created = 0
    stopped = False
    usage_total = UsageStats()
    usage_batches: List[UsageStats] = []
    if progress and total:
        progress(0.0, f"0/{total}")
    total_batches = len(batches)
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
            client,
            payload,
            model=model,
            system_instructions=system_instructions,
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
