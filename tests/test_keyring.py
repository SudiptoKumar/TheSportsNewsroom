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


if __name__ == "__main__":
    unittest.main()
