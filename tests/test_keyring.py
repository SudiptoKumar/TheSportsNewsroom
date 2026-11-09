from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cerebras_keys
import main

KEY1, KEY2 = "test-key-1", "test-key-2"
SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
    "additionalProperties": False,
}


class KeyringFailoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {k: os.environ.get(k) for k in ("CEREBRAS_API_KEY", "CEREBRAS_API_KEY_2")}
        os.environ["CEREBRAS_API_KEY"] = KEY1
        os.environ["CEREBRAS_API_KEY_2"] = KEY2
        cerebras_keys._reset_state()
        self._old_global = main.CEREBRAS_API_KEY
        main.CEREBRAS_API_KEY = KEY1
        self.calls: list[str] = []
        self.script: list[int] = []
        self._old_http, self._old_sleep = main.http, main.sleep
        main.http = self._fake_http
        main.sleep = lambda _s: None

    def tearDown(self) -> None:
        main.http, main.sleep = self._old_http, self._old_sleep
        main.CEREBRAS_API_KEY = self._old_global
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cerebras_keys._reset_state()

    def _fake_http(self, source, method, url, **kwargs):
        self.calls.append((kwargs.get("headers") or {}).get("Authorization", ""))
        status = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if status == 200:
            body = {"choices": [{"message": {"content": json.dumps({"a": "ok"})}, "finish_reason": "stop"}], "usage": {}}
            return main.Resp(200, data=body, text_="ok")
        return main.Resp(status, data={"error": {"message": "provider error"}}, text_="provider error")

    def _client(self) -> main.AIClient:
        client = main.AIClient()
        client._last_call = main.time.monotonic()
        return client

    def test_failover_to_second_key(self) -> None:
        self.script = [402, 200]
        out = self._client().json("task", "system", "user", SCHEMA)
        self.assertEqual(out, {"a": "ok"})
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0].endswith(KEY1), self.calls)
        self.assertTrue(self.calls[1].endswith(KEY2), self.calls)

    def test_non_quota_error_does_not_rotate(self) -> None:
        self.script = [400]
        out = self._client().json("task", "system", "user", SCHEMA)
        self.assertIsNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_both_keys_dead_returns_none(self) -> None:
        self.script = [402, 402]
        client = self._client()
        out = client.json("task", "system", "user", SCHEMA)
        self.assertIsNone(out)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(client.fatal)

    # --- 429 handling (2026-11-10): Sports routes only quota-exceeded 429s
    # to cerebras_keys.failover(); plain 429s are paced inside _json itself.

    def _quota_http(self, script):
        state = {"i": 0}

        def fake(source, method, url, **kwargs):
            self.calls.append((kwargs.get("headers") or {}).get("Authorization", ""))
            status = script[state["i"]] if state["i"] < len(script) else script[-1]
            state["i"] += 1
            if status == 200:
                body = {"choices": [{"message": {"content": json.dumps({"a": "ok"})}, "finish_reason": "stop"}], "usage": {}}
                return main.Resp(200, data=body, text_="ok")
            return main.Resp(status, data={"error": {"message": "quota exceeded"}}, text_="quota exceeded: tokens per minute")

        return fake

    def _run_429(self, script, capture_alerts=False):
        import urllib.parse

        main.http = self._quota_http(script)
        fake_time = _FakeTime()
        original_time = cerebras_keys.time
        cerebras_keys.time = fake_time
        sent = []
        original_urlopen = cerebras_keys.urlrequest.urlopen
        if capture_alerts:
            os.environ["NEWSROOM_BOT_TOKEN"] = "dummy-token"
            os.environ["ALERT_CHAT_ID"] = "12345"

            class _Resp:
                def read(self):
                    return b"{}"

            cerebras_keys.urlrequest.urlopen = lambda req, timeout=None: (sent.append(req.data.decode()), _Resp())[1]
        try:
            out = self._client().json("task", "system", "user", SCHEMA)
        finally:
            cerebras_keys.time = original_time
            cerebras_keys.urlrequest.urlopen = original_urlopen
            os.environ.pop("NEWSROOM_BOT_TOKEN", None)
            os.environ.pop("ALERT_CHAT_ID", None)
        texts = [urllib.parse.unquote_plus(s) for s in sent]
        return out, fake_time, texts

    def test_429_retries_same_key_then_succeeds(self) -> None:
        out, fake_time, texts = self._run_429([429, 200])
        self.assertEqual(out, {"a": "ok"})
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(c.endswith(KEY1) for c in self.calls), self.calls)
        self.assertEqual(fake_time.sleeps, [20])
        self.assertEqual(texts, [])

    def test_429_persistent_switches_with_throttle_alert(self) -> None:
        out, fake_time, texts = self._run_429([429, 429, 429, 429, 200], capture_alerts=True)
        self.assertEqual(out, {"a": "ok"})
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all(c.endswith(KEY1) for c in self.calls[:4]), self.calls)
        self.assertTrue(self.calls[4].endswith(KEY2), self.calls)
        self.assertEqual(fake_time.sleeps, [20, 40, 60])
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("rate-limited", texts[0])
        self.assertIn("key 1", texts[0])
        self.assertNotIn("finished", texts[0])

    def test_429_all_keys_rate_limited_alert(self) -> None:
        out, fake_time, texts = self._run_429([429], capture_alerts=True)
        self.assertIsNone(out)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("rate-limited", texts[0])
        self.assertIn("Switched to key 2", texts[0])
        self.assertIn("ALL Cerebras keys are rate-limited", texts[1])
        self.assertNotIn("are finished", texts[1])


class _FakeTime:
    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)


if __name__ == "__main__":
    unittest.main()
