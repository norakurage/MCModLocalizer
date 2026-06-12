"""Regression tests for SettingsStore (client_storage persistence)."""
from __future__ import annotations

import json
import unittest

from app.core.settings import SettingsStore
from app.core.usage import UsageStats


class FakeStorage:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def clear(self):
        self.data.clear()


class PrimitivesTest(unittest.TestCase):
    def test_get_set_clear(self):
        s = SettingsStore(FakeStorage())
        self.assertIsNone(s.get(SettingsStore.K_MODEL))
        s.set(SettingsStore.K_MODEL, "gemini-2.5-flash")
        self.assertEqual(s.get(SettingsStore.K_MODEL), "gemini-2.5-flash")
        s.clear()
        self.assertIsNone(s.get(SettingsStore.K_MODEL))


class UsageHistoryTest(unittest.TestCase):
    def test_empty_when_unset(self):
        self.assertEqual(SettingsStore(FakeStorage()).load_usage_history(), [])

    def test_roundtrip(self):
        s = SettingsStore(FakeStorage())
        history = [{"timestamp": "t", "model": "m", "prompt": 10, "completion": 5, "total": 15, "cost": 0.1}]
        s.save_usage_history(history)
        self.assertEqual(s.load_usage_history(), history)

    def test_coerces_and_fills_total(self):
        raw = json.dumps([
            {"timestamp": "t", "model": "m", "prompt": "10", "completion": "5", "cost": "0.25"},  # total missing
            {"prompt": "x", "completion": 3},  # bad prompt -> 0, total fallback
            "not-a-dict",  # skipped
        ])
        s = SettingsStore(FakeStorage({SettingsStore.K_USAGE_HISTORY: raw}))
        hist = s.load_usage_history()
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["prompt"], 10)
        self.assertEqual(hist[0]["total"], 15)  # 10 + 5 fallback
        self.assertEqual(hist[0]["cost"], 0.25)
        self.assertEqual(hist[1]["prompt"], 0)
        self.assertEqual(hist[1]["total"], 3)  # 0 + 3

    def test_invalid_json_discarded_with_warning(self):
        logs = []
        s = SettingsStore(FakeStorage({SettingsStore.K_USAGE_HISTORY: "{not json"}), log=logs.append)
        self.assertEqual(s.load_usage_history(), [])
        self.assertTrue(any("使用履歴の読み込みに失敗" in line for line in logs))

    def test_non_list_returns_empty(self):
        s = SettingsStore(FakeStorage({SettingsStore.K_USAGE_HISTORY: json.dumps({"x": 1})}))
        self.assertEqual(s.load_usage_history(), [])


class TotalCostTest(unittest.TestCase):
    def test_missing_is_zero(self):
        self.assertEqual(SettingsStore(FakeStorage()).load_total_cost(), 0.0)

    def test_invalid_is_zero(self):
        s = SettingsStore(FakeStorage({SettingsStore.K_USAGE_TOTAL_COST: "abc"}))
        self.assertEqual(s.load_total_cost(), 0.0)

    def test_roundtrip_six_decimals(self):
        store = FakeStorage()
        s = SettingsStore(store)
        s.save_total_cost(1.2345678)
        self.assertEqual(store.get(SettingsStore.K_USAGE_TOTAL_COST), "1.234568")
        self.assertAlmostEqual(s.load_total_cost(), 1.234568)


class TotalStatsTest(unittest.TestCase):
    def test_from_saved(self):
        raw = json.dumps({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        s = SettingsStore(FakeStorage({SettingsStore.K_USAGE_TOTAL_STATS: raw}))
        self.assertEqual(s.load_total_stats(), UsageStats(10, 5, 15))

    def test_fallback_sums_history_when_missing(self):
        s = SettingsStore(FakeStorage())
        history = [
            {"prompt": 10, "completion": 5, "total": 15},
            {"prompt": 2, "completion": 3, "total": 5},
        ]
        self.assertEqual(s.load_total_stats(history), UsageStats(12, 8, 20))

    def test_malformed_saved_falls_back(self):
        s = SettingsStore(FakeStorage({SettingsStore.K_USAGE_TOTAL_STATS: "{bad"}))
        self.assertEqual(s.load_total_stats([{"prompt": 1, "completion": 1, "total": 2}]), UsageStats(1, 1, 2))

    def test_roundtrip(self):
        store = FakeStorage()
        s = SettingsStore(store)
        s.save_total_stats(UsageStats(7, 8, 15))
        self.assertEqual(s.load_total_stats(), UsageStats(7, 8, 15))


if __name__ == "__main__":
    unittest.main()
