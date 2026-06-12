"""Regression tests for JAR reading and extract_localizations."""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.core.jar_reader import read_en_us_from_jar, read_lang_from_jar
from app.services.extraction import extract_localizations


def _make_jar(path: Path, modid: str, en: dict | None, ja: dict | None = None) -> None:
    with zipfile.ZipFile(path, "w") as z:
        if en is not None:
            z.writestr(f"assets/{modid}/lang/en_us.json", json.dumps(en, ensure_ascii=False))
        if ja is not None:
            z.writestr(f"assets/{modid}/lang/ja_jp.json", json.dumps(ja, ensure_ascii=False))


class JarReaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_en_and_ja(self):
        jar = self.root / "mod.jar"
        _make_jar(jar, "examplemod", {"a": "A"}, {"a": "あ"})
        self.assertEqual(read_en_us_from_jar(jar), {"examplemod": {"a": "A"}})
        self.assertEqual(read_lang_from_jar(jar, "ja_jp"), {"examplemod": {"a": "あ"}})

    def test_missing_locale_returns_empty(self):
        jar = self.root / "mod.jar"
        _make_jar(jar, "examplemod", {"a": "A"})
        self.assertEqual(read_lang_from_jar(jar, "ja_jp"), {})


class ExtractLocalizationsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mods = self.root / "mods"
        self.mods.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_en_and_existing_ja(self):
        _make_jar(self.mods / "mod.jar", "examplemod", {"a": "A", "b": "B"}, {"a": "あ"})
        result = extract_localizations(self.mods, self.out)
        self.assertEqual(result.mod_maps["examplemod"], {"a": "A", "b": "B"})
        self.assertEqual(result.existing_ja_maps["examplemod"], {"a": "あ"})
        en_path = self.out / "examplemod" / "en_us.json"
        self.assertTrue(en_path.exists())
        self.assertEqual(json.loads(en_path.read_text(encoding="utf-8")), {"a": "A", "b": "B"})
        self.assertTrue((self.out / "examplemod" / "ja_jp.json").exists())

    def test_no_jar_raises(self):
        with self.assertRaises(ValueError):
            extract_localizations(self.mods, self.out)


if __name__ == "__main__":
    unittest.main()
