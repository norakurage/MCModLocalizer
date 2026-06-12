"""Regression tests for run_translation_jobs (multi-mod orchestration).

translate_localizations is faked so these tests lock the per-target loop:
skip-existing, stop, per-mod error, pack building and usage aggregation.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.usage import UsageStats
from app.services import translation as tr
from app.services.translation import TranslationResult, parse_fraction


def make_fake_translate(stopped_for=None, raise_for=None):
    stopped_for = set(stopped_for or ())
    raise_for = set(raise_for or ())

    def fake(api_key=None, model=None, in_path=None, out_path=None,
             existing_translations=None, *, log=None, progress=None,
             should_stop=None, resume_path=None):
        modid = in_path.parent.name
        if modid in raise_for:
            raise RuntimeError("boom")
        if progress:
            progress(1.0, "1/1")
        if modid in stopped_for:
            return TranslationResult(total=1, created=0, out_path=out_path, stopped=True,
                                     usages=[UsageStats(2, 3, 5)],
                                     prompt_tokens=2, completion_tokens=3, total_tokens=5)
        out_path.write_text(json.dumps({"a": "あ"}, ensure_ascii=False), encoding="utf-8")
        return TranslationResult(total=1, created=1, out_path=out_path, stopped=False,
                                 usages=[UsageStats(2, 3, 5)],
                                 prompt_tokens=2, completion_tokens=3, total_tokens=5)

    return fake


class RunTranslationJobsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        self.logs: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _target(self, modid: str):
        d = self.src / modid
        d.mkdir(parents=True, exist_ok=True)
        en = d / "en_us.json"
        en.write_text(json.dumps({"a": "A"}), encoding="utf-8")
        return (modid, en, {})

    def _run(self, targets, **kw):
        defaults = dict(
            api_key="key",
            model="gemini-2.5-flash-lite",
            output_dir=self.out,
            build_pack=mock.Mock(return_value=self.out / "pack"),
            log=self.logs.append,
            should_stop=lambda: False,
        )
        defaults.update(kw)
        return tr.run_translation_jobs(targets, **defaults), defaults["build_pack"]

    def test_translates_all_targets(self):
        targets = [self._target("moda"), self._target("modb")]
        with mock.patch.object(tr, "translate_localizations", side_effect=make_fake_translate()):
            summary, build_pack = self._run(targets)
        self.assertEqual(summary.translated_mods, 2)
        self.assertEqual(summary.total_mods, 2)
        self.assertFalse(summary.aborted)
        self.assertFalse(summary.had_error)
        self.assertEqual(summary.model, "gemini-2.5-flash-lite")
        # usage aggregated across both mods (2 calls x (2,3,5))
        self.assertEqual(summary.prompt_tokens, 4)
        self.assertEqual(summary.completion_tokens, 6)
        self.assertEqual(len(summary.usage_records), 2)
        build_pack.assert_called()
        self.assertEqual(summary.pack_dir, self.out / "pack")

    def test_skips_mod_with_existing_pack_translation(self):
        # existing translation already present in output_dir for "skipmod"
        existing = self.out / "somepack" / "assets" / "skipmod" / "lang" / "ja_jp.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("{}", encoding="utf-8")
        targets = [self._target("skipmod"), self._target("newmod")]
        fake = make_fake_translate()
        with mock.patch.object(tr, "translate_localizations", side_effect=fake) as m:
            summary, _ = self._run(targets)
        # only newmod is translated; skipmod is skipped
        self.assertEqual(summary.translated_mods, 1)
        self.assertEqual(m.call_count, 1)
        self.assertTrue(any("スキップします（skipmod）" in line for line in self.logs))

    def test_stop_before_any_work(self):
        targets = [self._target("moda")]
        with mock.patch.object(tr, "translate_localizations", side_effect=make_fake_translate()) as m:
            summary, build_pack = self._run(targets, should_stop=lambda: True)
        self.assertTrue(summary.aborted)
        self.assertEqual(summary.translated_mods, 0)
        m.assert_not_called()
        build_pack.assert_not_called()

    def test_stop_reported_by_translate(self):
        targets = [self._target("moda"), self._target("modb")]
        fake = make_fake_translate(stopped_for={"moda"})
        with mock.patch.object(tr, "translate_localizations", side_effect=fake):
            summary, _ = self._run(targets)
        self.assertTrue(summary.aborted)
        self.assertEqual(summary.translated_mods, 0)  # moda stopped, modb never reached

    def test_per_mod_error_marks_had_error(self):
        targets = [self._target("moda")]
        fake = make_fake_translate(raise_for={"moda"})
        with mock.patch.object(tr, "translate_localizations", side_effect=fake):
            summary, _ = self._run(targets)
        self.assertTrue(summary.had_error)
        self.assertTrue(summary.aborted)
        self.assertEqual(summary.translated_mods, 0)

    def test_final_pack_build_when_loop_never_built(self):
        # build_pack returns None during the loop -> final block builds once
        targets = [self._target("moda")]
        build_pack = mock.Mock(side_effect=[None, self.out / "pack"])
        with mock.patch.object(tr, "translate_localizations", side_effect=make_fake_translate()):
            summary, _ = self._run(targets, build_pack=build_pack)
        self.assertEqual(build_pack.call_count, 2)
        self.assertEqual(summary.pack_dir, self.out / "pack")


class ParseFractionTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_fraction("3/10"), (3, 10))
        self.assertEqual(parse_fraction(" 7 / 12 "), (7, 12))

    def test_invalid(self):
        self.assertIsNone(parse_fraction("50%"))
        self.assertIsNone(parse_fraction("a/b"))


if __name__ == "__main__":
    unittest.main()
