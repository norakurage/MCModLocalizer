"""Regression tests for pure core helpers (tokens, chunking, usage, cost)."""
from __future__ import annotations

import unittest

from app.core.chunking import chunk_pairs
from app.core.token_protection import protect_tokens, restore_tokens
from app.core.usage import UsageStats, estimate_cost, usage_from_response


class TokenProtectionTest(unittest.TestCase):
    def test_roundtrip(self):
        s = "Press %1$s for §a {name} and \\n done"
        protected, mapping = protect_tokens(s)
        self.assertNotEqual(protected, s)
        self.assertTrue(mapping)
        self.assertIn("‹T0›", protected)
        self.assertEqual(restore_tokens(protected, mapping), s)

    def test_no_tokens(self):
        protected, mapping = protect_tokens("plain text")
        self.assertEqual(protected, "plain text")
        self.assertEqual(mapping, {})


class ChunkPairsTest(unittest.TestCase):
    def test_splits_by_max_items(self):
        pairs = [(f"k{i}", f"v{i}") for i in range(5)]
        chunks = list(chunk_pairs(pairs, max_chars=10_000, max_items=2))
        self.assertEqual([len(c) for c in chunks], [2, 2, 1])

    def test_splits_by_max_chars(self):
        import json as _json

        pairs = [(f"k{i}", "x" * 50) for i in range(4)]
        one = len(_json.dumps({"key": "k0", "value": "x" * 50}, ensure_ascii=False))
        # threshold holds exactly one item but not two
        chunks = list(chunk_pairs(pairs, max_chars=one + 5, max_items=100))
        self.assertEqual([len(c) for c in chunks], [1, 1, 1, 1])
        # order is preserved across chunks
        self.assertEqual([k for c in chunks for k, _ in c], [f"k{i}" for i in range(4)])

    def test_empty(self):
        self.assertEqual(list(chunk_pairs([])), [])


class EstimateCostTest(unittest.TestCase):
    def test_basic(self):
        pricing = {"input": 0.10, "cached_input": 0.01, "output": 0.40}
        self.assertAlmostEqual(estimate_cost(pricing, 1_000_000, 1_000_000), 0.50)
        self.assertAlmostEqual(estimate_cost(pricing, 500_000, 250_000), 0.10 * 0.5 + 0.40 * 0.25)

    def test_no_pricing(self):
        self.assertEqual(estimate_cost(None, 999, 999), 0.0)
        self.assertEqual(estimate_cost({}, 999, 999), 0.0)


class UsageFromResponseTest(unittest.TestCase):
    def test_standard_keys(self):
        u = usage_from_response({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self.assertEqual((u.prompt_tokens, u.completion_tokens, u.total_tokens), (10, 5, 15))

    def test_alias_keys_with_total_fallback(self):
        u = usage_from_response({"usage": {"input_tokens": 10, "output_tokens": 5}})
        self.assertEqual((u.prompt_tokens, u.completion_tokens, u.total_tokens), (10, 5, 15))

    def test_missing_usage(self):
        self.assertEqual(usage_from_response({}), UsageStats())


if __name__ == "__main__":
    unittest.main()
