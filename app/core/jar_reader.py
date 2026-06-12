"""Helpers for reading localization data from JAR files."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Dict

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


__all__ = [
    "MOD_LANG_PATTERN",
    "read_lang_from_jar",
    "read_en_us_from_jar",
]
