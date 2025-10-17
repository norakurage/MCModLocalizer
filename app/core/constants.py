"""Static constants used across the application."""
from __future__ import annotations

import re

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

__all__ = [
    "PLACEHOLDER_PATTERNS",
    "COLOR_CODES",
    "ESCAPES",
    "PROTECT_RE",
    "SYSTEM_INSTRUCTIONS_BASE",
    "USER_TEMPLATE",
]
