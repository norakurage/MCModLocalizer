"""Compatibility layer for legacy processing imports."""
from __future__ import annotations

from .core import (
    SYSTEM_INSTRUCTIONS_BASE,
    USER_TEMPLATE,
    UsageStats,
    protect_tokens,
    restore_tokens,
)
from .core.chunking import chunk_pairs
from .core.constants import COLOR_CODES, ESCAPES, PLACEHOLDER_PATTERNS, PROTECT_RE
from .core.jar_reader import (
    MOD_LANG_PATTERN,
    choose_primary_modid,
    read_en_us_from_jar,
    read_lang_from_jar,
)
from .core.json_io import load_json, write_json
from .core.translation_batch import translate_batch
from .core.usage import usage_from_response as _usage_from_response
from .services.extraction import ExtractionResult, extract_localizations
from .services.translation import TranslationResult, translate_localizations

__all__ = [
    "SYSTEM_INSTRUCTIONS_BASE",
    "USER_TEMPLATE",
    "UsageStats",
    "protect_tokens",
    "restore_tokens",
    "chunk_pairs",
    "COLOR_CODES",
    "ESCAPES",
    "PLACEHOLDER_PATTERNS",
    "PROTECT_RE",
    "MOD_LANG_PATTERN",
    "choose_primary_modid",
    "read_en_us_from_jar",
    "read_lang_from_jar",
    "load_json",
    "write_json",
    "translate_batch",
    "_usage_from_response",
    "ExtractionResult",
    "extract_localizations",
    "TranslationResult",
    "translate_localizations",
]
