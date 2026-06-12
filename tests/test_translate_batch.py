"""Regression tests for translate_batch HTTP/parse/retry behaviour.

urllib is faked so no network is touched. These lock the retry policy:
429/5xx -> retry with backoff, 400/401/403 -> immediate raise.
"""
from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from app.core import translation_batch as tb


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_body(items, usage=None):
    payload = {"choices": [{"message": {"content": json.dumps({"items": items}, ensure_ascii=False)}}]}
    if usage:
        payload["usage"] = usage
    return json.dumps(payload)


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, f"status {code}", {}, None)


class TranslateBatchTest(unittest.TestCase):
    def test_parses_items_and_usage(self):
        items = [{"key": "k1", "value": "v1"}, {"key": "k2", "value": "v2"}]
        body = _ok_body(["A", "B"], {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            out, usage = tb.translate_batch("key", items, model="m", system_instructions="sys")
        self.assertEqual(out, {"k1": "A", "k2": "B"})
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.total_tokens), (3, 4, 7))

    def test_retries_on_503_then_succeeds(self):
        items = [{"key": "k1", "value": "v1"}]
        with mock.patch("app.core.translation_batch.time.sleep") as sleep, mock.patch(
            "urllib.request.urlopen", side_effect=[_http_error(503), _FakeResp(_ok_body(["A"]))]
        ) as urlopen:
            out, _ = tb.translate_batch("key", items, model="m", system_instructions="sys")
        self.assertEqual(out, {"k1": "A"})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called()

    def test_raises_immediately_on_400(self):
        items = [{"key": "k1", "value": "v1"}]
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                tb.translate_batch("key", items, model="m", system_instructions="sys")
        self.assertEqual(urlopen.call_count, 1)

    def test_empty_items_short_circuits(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            out, usage = tb.translate_batch("key", [], model="m", system_instructions="sys")
        self.assertEqual(out, {})
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
