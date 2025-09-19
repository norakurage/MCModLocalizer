from __future__ import annotations

import json
import re
import time
import zipfile
from dataclasses import dataclass
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
- 返答は必ず JSON（キー:値のオブジェクト）で返す
"""

USER_TEMPLATE = """以下の items は { \"key\":..., \"value\":... } の配列です。
出力は **単一の JSON オブジェクトのみ** とし、構造は次の通りです。
- 各 item.key をそのままキーにする（文字列を一切変更しない）
- 値は item.value の日本語訳（保護トークン ‹Tn› は原文どおりそのまま残す）
【入力例】
items:
[
  {\"key\":\"block.example.copper_block\",\"value\":\"Copper Block\"},
  {\"key\":\"message.example.tips\",\"value\":\"Press ‹T0› to open the menu.\"}
]
【出力例】（この形式以外は出力しない）
{
  \"block.example.copper_block\": \"銅のブロック\",
  \"message.example.tips\": \"メニューを開くには ‹T0› を押します。\"
}
items:
<<PAYLOAD>>
"""

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]
StopFn = Callable[[], bool]


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


def translate_batch(client: OpenAI, items: List[Dict[str, str]], model: str, system_instructions: str) -> Dict[str, str]:
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    user_text = USER_TEMPLATE.replace("<<PAYLOAD>>", payload)
    expected_keys = [it["key"] for it in items]
    unique_keys = list(dict.fromkeys(expected_keys))
    if not unique_keys:
        return {}
    schema_props = {k: {"type": "string"} for k in unique_keys}
    response_format_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "translation_map",
            "schema": {
                "type": "object",
                "properties": schema_props,
                "required": unique_keys,
                "additionalProperties": False,
            },
        },
    }
    expected_key_set = set(unique_keys)
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

    def _parse_any(out: str) -> Dict[str, str]:
        try:
            obj = json.loads(out)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
            if isinstance(obj, list):
                m: Dict[str, str] = {}
                for r in obj:
                    if isinstance(r, dict) and "key" in r:
                        val = r.get("ja") or r.get("value_ja") or r.get("value") or ""
                        if val:
                            m[str(r["key"])] = str(val)
                if m:
                    return m
        except Exception:
            pass
        m = re.search(r"\{.*\}", out or "", re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return {str(k): str(v) for k, v in obj.items()}
            except Exception:
                pass
        return {}

    def _call_responses(with_response_format: bool, extra_note: str = "") -> Dict[str, str]:
        nonlocal last_raw
        args = dict(
            model=model,
            instructions=system_instructions + extra_note,
            input=user_text,
            temperature=0.2,
        )
        if with_response_format:
            args["response_format"] = response_format_schema
        try:
            resp = client.responses.create(**args)  # type: ignore[arg-type]
        except TypeError:
            if with_response_format:
                return _call_responses(
                    False,
                    extra_note + "\n出力は必ず『単一の JSON オブジェクト（item.key→日本語訳）』のみで返してください。"
                )
            raise
        out = _extract_text(resp)
        last_raw = out or ""
        return _parse_any(out)

    def _call_chat(extra_note: str = "") -> Dict[str, str]:
        nonlocal last_raw
        messages = [
            {"role": "system", "content": system_instructions + extra_note},
            {"role": "user", "content": user_text},
        ]
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = ""
        if getattr(resp, "choices", None):
            msg = resp.choices[0].message
            content = getattr(msg, "content", None) or ""
        last_raw = content or ""
        return _parse_any(content or "")

    data = _call_responses(True)
    inter = expected_key_set.intersection(data.keys())
    if len(inter) < len(expected_key_set):
        note = ("\n出力は次の形式のみ：{<item.key>: <日本語訳>}。キー名 'key' や 'value' を出力キーとして使わないこと。余計な文字や説明は一切書かないこと。")
        inter = expected_key_set.intersection(data.keys())
        if len(inter) < len(expected_key_set):
            data = _call_chat(note)
            inter = expected_key_set.intersection(data.keys())
    if len(inter) < len(expected_key_set):
        missing = [k for k in unique_keys if not data.get(k)]
        snippet = (last_raw or "").strip().replace("\r", " ").replace("\n", " ")[:400]
        raise RuntimeError(f"LLM output missing {len(missing)} keys (expected {len(unique_keys)}). Raw snippet: {snippet}")
    return {str(k): str(data.get(k, "")) for k in expected_keys}


def load_json(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


MOD_LANG_RE = re.compile(r"^assets/([^/]+)/lang/en_us\.json$")


def read_en_us_from_jar(jar_path: Path) -> Dict[str, Dict[str, str]]:
    """JAR 内の assets/<modid>/lang/en_us.json を全て読み取る。"""
    out: Dict[str, Dict[str, str]] = {}
    with zipfile.ZipFile(jar_path, "r") as zf:
        for name in zf.namelist():
            m = MOD_LANG_RE.match(name)
            if not m:
                continue
            modid = m.group(1)
            try:
                with zf.open(name) as f:
                    data = json.loads(f.read().decode("utf-8"))
                out[modid] = {str(k): str(v) for k, v in data.items()}
            except Exception:
                pass
    return out


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


def make_skeleton(en_map: Dict[str, str]) -> Dict[str, str]:
    return {k: "" for k in en_map.keys()}


@dataclass
class ExtractionResult:
    primary_modid: Optional[str]
    primary_en_path: Optional[Path]
    mod_maps: Dict[str, Dict[str, str]]


@dataclass
class TranslationResult:
    total: int
    created: int
    out_path: Path
    stopped: bool


def extract_localizations(
    jar_path: Path,
    out_dir: Path,
    *,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
) -> ExtractionResult:
    mod_maps = read_en_us_from_jar(jar_path)
    if not mod_maps:
        raise ValueError("JAR 内に assets/<modid>/lang/en_us.json が見つかりませんでした。")
    if len(mod_maps) > 1 and log:
        mods = ", ".join(f"{m}({len(d)} keys)" for m, d in mod_maps.items())
        log(f"[WARN] 複数の namespace が見つかりました -> {mods}。全て出力します。")
    total = len(mod_maps)
    done = 0
    primary_modid: Optional[str] = None
    primary_en_path: Optional[Path] = None
    primary_map: Optional[Dict[str, str]] = None
    if mod_maps:
        primary_modid, primary_map = choose_primary_modid(mod_maps)
    for modid, en_map in mod_maps.items():
        mod_dir = out_dir / modid
        en_path = mod_dir / "en_us.json"
        skel_path = mod_dir / "ja_jp.skeleton.json"
        write_json(en_path, en_map)
        write_json(skel_path, make_skeleton(en_map))
        if log:
            log(f"[OK] 抽出: {modid} -> {en_path}")
        if primary_modid == modid:
            primary_en_path = en_path
        done += 1
        if progress:
            progress(done / total, f"{done}/{total}")
    if log and primary_modid and primary_map is not None:
        log(f"[INFO] 代表 modid: {primary_modid}（キー数: {len(primary_map)}）")
        log("[OK] ひな形 (ja_jp.skeleton.json) を作成しました。")
    if progress:
        progress(1.0, f"{total}/{total}")
    return ExtractionResult(primary_modid=primary_modid, primary_en_path=primary_en_path, mod_maps=mod_maps)


def translate_localizations(
    api_key: str,
    model: str,
    in_path: Path,
    out_path: Path,
    *,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[StopFn] = None,
    sleep_interval: float = 0.4,
) -> TranslationResult:
    if should_stop is None:
        should_stop = lambda: False
    if log:
        log(f"[RUN] 入力: {in_path}")
        log(f"[RUN] 出力: {out_path}")
    src: Dict[str, str] = load_json(in_path)
    dst: Dict[str, str] = load_json(out_path)
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
    batches = list(chunk_pairs(todo))
    total = sum(len(batch) for batch in batches)
    if total == 0:
        if log:
            log("[OK] すでに翻訳済みです（差分なし）。")
        if progress:
            progress(1.0, "完了")
        return TranslationResult(total=0, created=0, out_path=out_path, stopped=False)
    system_instructions = SYSTEM_INSTRUCTIONS_BASE
    client = OpenAI(api_key=api_key)
    created = 0
    stopped = False
    if progress and total:
        progress(0.0, f"0/{total}")
    for batch in batches:
        if should_stop():
            stopped = True
            if log:
                log("[STOP] ユーザーによって停止されました。現在までの結果を保存します。")
            break
        kv: Dict[str, Tuple[str, Dict[str, str]]] = {}
        payload: List[Dict[str, str]] = []
        for k, protected in batch:
            protected2, m = protect_tokens(protected)
            kv[k] = (protected2, m)
            payload.append({"key": k, "value": protected2})
        out_map = translate_batch(client, payload, model=model, system_instructions=system_instructions)
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
            log(f"[INFO] バッチ完了: {created}/{total}")
        if sleep_interval > 0:
            time.sleep(sleep_interval)
    write_json(out_path, dst)
    if log:
        log(f"[OK] 書き込み完了: {out_path}")
    if progress:
        final_ratio = created / max(1, total)
        progress(1.0 if not stopped else final_ratio, f"{created}/{total}")
    return TranslationResult(total=total, created=created, out_path=out_path, stopped=stopped)
