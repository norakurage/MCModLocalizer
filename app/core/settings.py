"""Persistent application settings backed by Flet client_storage.

client_storage への素の読み書きと、トークン使用量データ（履歴・累計・概算
コスト）の (de)シリアライズを 1 箇所に集約する。UI 非依存で、保存先は
get/set/clear を備えた任意のオブジェクトを注入できる（テストでは dict ベース
のフェイクを利用）。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from .usage import UsageStats

LogFn = Optional[Callable[[str], None]]


def _to_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return 0


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except Exception:
        return 0.0


class SettingsStore:
    """client_storage への永続化と使用量データの (de)シリアライズを担う。"""

    K_MODEL = "openai_model"
    K_DIR_MODS = "dir_mods_root"
    K_DIR_OUTPUT = "dir_output_pack"
    K_LAST_MODS_PATH = "last_mods_dir_path"
    K_LAST_OUTPUT_PATH = "last_output_dir_path"
    K_USAGE_HISTORY = "token_usage_history"
    K_USAGE_TOTAL_COST = "token_usage_total_cost"
    K_USAGE_TOTAL_STATS = "token_usage_total_stats"

    def __init__(self, storage: Any, *, log: LogFn = None) -> None:
        self._storage = storage
        self._log = log

    def _emit(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    # ------------------------------
    # 素の読み書き
    # ------------------------------
    def get(self, key: str) -> Optional[str]:
        return self._storage.get(key)

    def set(self, key: str, value: str) -> None:
        self._storage.set(key, value)

    def clear(self) -> None:
        self._storage.clear()

    # ------------------------------
    # 使用量履歴
    # ------------------------------
    def load_usage_history(self) -> List[Dict[str, object]]:
        raw = self._storage.get(self.K_USAGE_HISTORY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            self._emit("[WARN] トークン使用履歴の読み込みに失敗しました。データを破棄します。")
            return []
        if not isinstance(data, list):
            return []
        history: List[Dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp", ""))
            model = str(item.get("model", ""))
            prompt_i = _to_int(item.get("prompt", 0))
            completion_i = _to_int(item.get("completion", 0))
            total = item.get("total", 0)
            try:
                total_i = int(total) if total else prompt_i + completion_i
            except Exception:
                total_i = prompt_i + completion_i
            cost_f = _to_float(item.get("cost", 0.0))
            history.append(
                {
                    "timestamp": ts,
                    "model": model,
                    "prompt": prompt_i,
                    "completion": completion_i,
                    "total": total_i,
                    "cost": cost_f,
                }
            )
        return history

    def save_usage_history(self, history: List[Dict[str, object]]) -> None:
        try:
            payload = json.dumps(history, ensure_ascii=False)
            self._storage.set(self.K_USAGE_HISTORY, payload)
        except Exception as ex:
            self._emit(f"[WARN] トークン使用履歴の保存に失敗しました: {repr(ex)}")

    # ------------------------------
    # 累計コスト
    # ------------------------------
    def load_total_cost(self) -> float:
        raw = self._storage.get(self.K_USAGE_TOTAL_COST)
        if not raw:
            return 0.0
        try:
            return float(raw)
        except Exception:
            return 0.0

    def save_total_cost(self, cost: float) -> None:
        try:
            self._storage.set(self.K_USAGE_TOTAL_COST, f"{cost:.6f}")
        except Exception as ex:
            self._emit(f"[WARN] トークン累計コストの保存に失敗しました: {repr(ex)}")

    # ------------------------------
    # 累計使用量
    # ------------------------------
    def load_total_stats(self, history_fallback: Optional[List[Dict[str, object]]] = None) -> UsageStats:
        raw = self._storage.get(self.K_USAGE_TOTAL_STATS)
        if raw:
            try:
                data = json.loads(raw)
                return UsageStats(
                    prompt_tokens=int(data.get("prompt_tokens", 0)),
                    completion_tokens=int(data.get("completion_tokens", 0)),
                    total_tokens=int(data.get("total_tokens", 0)),
                )
            except Exception:
                pass

        # 保存済みが無ければ履歴から再計算する
        stats = UsageStats()
        for record in history_fallback or []:
            stats.prompt_tokens += _to_int(record.get("prompt", 0))
            stats.completion_tokens += _to_int(record.get("completion", 0))
            stats.total_tokens += _to_int(record.get("total", 0))
        return stats

    def save_total_stats(self, stats: UsageStats) -> None:
        try:
            data = {
                "prompt_tokens": stats.prompt_tokens,
                "completion_tokens": stats.completion_tokens,
                "total_tokens": stats.total_tokens,
            }
            self._storage.set(self.K_USAGE_TOTAL_STATS, json.dumps(data))
        except Exception as ex:
            self._emit(f"[WARN] トークン累計使用量の保存に失敗しました: {repr(ex)}")


__all__ = ["SettingsStore"]
