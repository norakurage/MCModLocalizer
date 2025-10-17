"""Extraction utilities for localization files."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..core.jar_reader import read_en_us_from_jar, read_lang_from_jar
from ..core.json_io import write_json

LogFn = Optional[Callable[[str], None]]
ProgressFn = Optional[Callable[[float, str], None]]


@dataclass
class ExtractionResult:
    primary_modid: Optional[str]
    primary_en_path: Optional[Path]
    mod_maps: Dict[str, Dict[str, str]]
    mod_sources: Dict[str, Path] = field(default_factory=dict)
    existing_ja_maps: Dict[str, Dict[str, str]] = field(default_factory=dict)


def extract_localizations(
    source_path: Path,
    out_dir: Path,
    *,
    log: LogFn = None,
    progress: ProgressFn = None,
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


__all__ = ["ExtractionResult", "extract_localizations"]
