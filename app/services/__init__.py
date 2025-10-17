"""翻訳関連のサービス群。"""

from .localization import (
    ExtractionResult,
    TranslationResult,
    UsageStats,
    chunk_pairs,
    extract_localizations,
    protect_tokens,
    restore_tokens,
    translate_batch,
    translate_localizations,
)

__all__ = [
    "ExtractionResult",
    "TranslationResult",
    "UsageStats",
    "chunk_pairs",
    "extract_localizations",
    "protect_tokens",
    "restore_tokens",
    "translate_batch",
    "translate_localizations",
]
