"""Regression tests for translate_localizations orchestration.

The network call (translate_batch) is faked so these tests lock the diff/merge/
resume/stop behaviour that the UI's _translate_targets relies on.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.usage import UsageStats
from app.services import translation as tr


def fake_translate_batch(api_key, payload, model, system_instructions, log_fn=None, _retry_depth=0):
    """Echo translator: prefixes each protected value with '訳:'."""
    out = {item["key"]: "訳:" + item["value"] for item in payload}
    return out, UsageStats(prompt_tokens=2, completion_tokens=3, total_tokens=5)


class TranslateFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.in_p = self.dir / "en_us.json"
        self.out_p = self.dir / "ja_jp.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path: Path, data: dict) -> Path:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def _dst(self) -> dict:
        return json.loads(self.out_p.read_text(encoding="utf-8"))

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_translates_only_missing_keys(self, _m):
        self._write(self.in_p, {"a": "A", "b": "B"})
        self._write(self.out_p, {"a": "既訳"})
        res = tr.translate_localizations("key", "model", self.in_p, self.out_p, sleep_interval=0)
        self.assertEqual(res.total, 1)
        self.assertEqual(res.created, 1)
        dst = self._dst()
        self.assertEqual(dst["a"], "既訳")
        self.assertEqual(dst["b"], "訳:B")

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_existing_translations_merged_when_dst_empty(self, _m):
        self._write(self.in_p, {"a": "A", "b": "B"})
        res = tr.translate_localizations(
            "key", "model", self.in_p, self.out_p,
            existing_translations={"a": "既訳A"}, sleep_interval=0,
        )
        self.assertEqual(res.total, 1)  # only b is missing
        dst = self._dst()
        self.assertEqual(dst["a"], "既訳A")
        self.assertEqual(dst["b"], "訳:B")

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_protected_tokens_restored(self, _m):
        self._write(self.in_p, {"a": "Press %s now"})
        tr.translate_localizations("key", "model", self.in_p, self.out_p, sleep_interval=0)
        dst = self._dst()
        self.assertIn("%s", dst["a"])
        self.assertNotIn("‹T0›", dst["a"])

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_nothing_to_do_returns_zero(self, _m):
        self._write(self.in_p, {"a": "A"})
        self._write(self.out_p, {"a": "既訳"})
        res = tr.translate_localizations("key", "model", self.in_p, self.out_p, sleep_interval=0)
        self.assertEqual((res.total, res.created), (0, 0))
        self.assertFalse(res.stopped)

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_stop_before_first_batch(self, _m):
        self._write(self.in_p, {"a": "A", "b": "B"})
        res = tr.translate_localizations(
            "key", "model", self.in_p, self.out_p,
            should_stop=lambda: True, sleep_interval=0,
        )
        self.assertTrue(res.stopped)
        self.assertEqual(res.created, 0)

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_resume_removed_when_complete(self, _m):
        self._write(self.in_p, {"a": "A"})
        resume = self.dir / ".resume" / "examplemod" / "ja_jp.json"
        resume.parent.mkdir(parents=True)
        resume.write_text("{}", encoding="utf-8")
        tr.translate_localizations(
            "key", "model", self.in_p, self.out_p,
            resume_path=resume, sleep_interval=0,
        )
        self.assertFalse(resume.exists())

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_resume_written_when_stopped(self, _m):
        self._write(self.in_p, {"a": "A", "b": "B"})
        resume = self.dir / ".resume" / "examplemod" / "ja_jp.json"
        res = tr.translate_localizations(
            "key", "model", self.in_p, self.out_p,
            should_stop=lambda: True, resume_path=resume, sleep_interval=0,
        )
        self.assertTrue(res.stopped)
        self.assertTrue(resume.exists())

    @mock.patch.object(tr, "translate_batch", side_effect=fake_translate_batch)
    def test_usage_aggregated(self, _m):
        self._write(self.in_p, {"a": "A", "b": "B"})
        res = tr.translate_localizations("key", "model", self.in_p, self.out_p, sleep_interval=0)
        # single batch -> one fake call worth of usage
        self.assertEqual(res.prompt_tokens, 2)
        self.assertEqual(res.completion_tokens, 3)
        self.assertEqual(res.total_tokens, 5)
        self.assertEqual(len(res.usages), 1)


if __name__ == "__main__":
    unittest.main()
