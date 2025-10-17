"""Service layer for localization workflows."""

from .extraction import ExtractionResult, extract_localizations
from .translation import TranslationResult, translate_localizations

__all__ = [
    "ExtractionResult",
    "TranslationResult",
    "extract_localizations",
    "translate_localizations",
]
