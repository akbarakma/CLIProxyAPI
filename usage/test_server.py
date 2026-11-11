import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

CONFIG = '''api-keys:
    openai-compatibility:
        - base-url: "https://oni.lio.id/v1"
          keys:
            - api-key: "juxta-key"
          name: "Juxta"
        - base-url: "https://api.z0ne.ai/v1"
          disabled: false
          headers:
            User-Agent: "cli-proxy-openai-compat"
          keys:
            - api-key: "z0ne-key"
          name: "z0ne"
'''

directory = tempfile.TemporaryDirectory(prefix="cliproxyapi-usage-test-")
root = Path(directory.name)
(root / "accounts.json").write_text(json.dumps([{"id": "codex-alt", "provider": "codex", "label": "Codex (Alt)", "file": "codex.json"}]))
(root / "config.yaml").write_text(CONFIG)
os.environ.update(USAGE_ALLOWED_LOGIN="owner@example.com", USAGE_ACCOUNTS=str(root / "accounts.json"), USAGE_PROXY_CONFIG=str(root / "config.yaml"))

import server  # noqa: E402


class ClaudeTests(unittest.TestCase):
    account = {"plan": "pro", "fable_included_until": "2026-11-17T07:59:00Z"}
    login = {"access_token": "fixture-token"}
    now = 1794000000

    def usage(self, raw, account=None):
        with patch.object(server, "request", return_value=({}, raw)), patch.object(server.time, "time", return_value=self.now):
            return server.claude_usage(account or self.account, self.login)

    def test_scoped_fable_has_its_own_percentage_reset_and_optional_expiry(self):
        raw = {"five_hour": {"utilization": 20}, "seven_day": {"utilization": 35},
               "limits": [{"kind": "weekly_scoped", "percent": 64, "resets_at": "2026-11-14T15:00:00Z",
                           "scope": {"model": {"display_name": "Fable"}}}]}
        reading = self.usage(raw)
        self.assertEqual([w["label"] for w in reading["windows"]], ["Session", "Weekly", "Fable"])
        fable = reading["windows"][-1]
        self.assertEqual(fable["usedPercent"], 64)
        self.assertEqual(fable["resetAt"], server.ms("2026-11-14T15:00:00Z"))
        self.assertEqual(fable["expiresAt"], server.ms(self.account["fable_included_until"]))
        self.assertNotIn("expiresAt", self.usage(raw, {"plan": "max"})["windows"][-1])
        self.assertNotIn("resetGrants", reading)
        with_opus = self.usage({**raw, "seven_day_opus": {"utilization": 10}})
        self.assertEqual([w["label"] for w in with_opus["windows"][:3]], ["Session", "Weekly", "Fable"])

    def test_missing_or_expired_fable_preserves_the_shared_windows(self):
        raw = {"five_hour": {"utilization": 20}, "seven_day": {"utilization": 35},
               "limits": [{"kind": "weekly_scoped", "percent": 64, "scope": {"model": {"display_name": "Fable"}}}]}
        for limits in [None, [], [None], [{"kind": "weekly_scoped", "percent": None}]]:
            self.assertEqual(len(self.usage({**raw, "limits": limits})["windows"]), 2)
        with patch.object(server.time, "time", return_value=server.ms(self.account["fable_included_until"]) / 1000):
            self.assertIsNone(server.fable_window(self.account, 64, None))
            self.assertIsNotNone(server.fable_window({"plan": "max"}, 64, None))

    def test_reset_effects_distinguish_fable_from_full_even_when_both_types_are_full(self):
        shared = {"type": "full", "resets_left": 1, "ends_at": "2026-11-17T07:59:00Z", "usable_now": True}
        raw = {"cedar_ember": {"eligible": True, "grants": [
            {**shared, "id": "fable", "gives_back_weekly": True, "clears": ["five_hour", "seven_day_overage_included"]},
            {**shared, "id": "full", "clears": ["five_hour", "seven_day", "seven_day_overage_included"]},
            {**shared, "id": "spent", "resets_left": 0, "clears": ["seven_day"]},
            {**shared, "id": "paused", "paused": True, "clears": ["five_hour"]},
            {"id": "malformed", "resets_left": -1},
        ]}}
        grants = self.usage(raw)["resetGrants"]["grants"]
        self.assertEqual([g["label"] for g in grants], ["Fable reset", "Full reset", "Weekly reset", "Session reset"])
        self.assertEqual(grants[2]["remainingCount"], 0)
        self.assertTrue(grants[3]["paused"])
        self.assertNotIn("resetGrants", self.usage({"cedar_ember": {"eligible": False, "grants": []}}))
        self.assertEqual(self.usage({"cedar_ember": {"eligible": True, "grants": []}})["resetGrants"]["grants"], [])

    def test_setup_probe_reads_all_three_windows_including_zero_fable_usage(self):
        headers = {"anthropic-ratelimit-unified-5h-utilization": "0.03", "anthropic-ratelimit-unified-7d-utilization": "0.68",
                   "anthropic-ratelimit-unified-7d_oi-utilization": "0", "anthropic-ratelimit-unified-7d_oi-reset": "1794520800"}
        with patch.object(server, "request", return_value=(headers, {})) as request:
            reading = server.claude_probe({"plan": "max"}, self.login)
        self.assertEqual(request.call_args.args[2]["model"], "claude-fable-5-2")
        self.assertEqual(request.call_args.args[2]["max_tokens"], 1)
        self.assertEqual([w["usedPercent"] for w in reading["windows"]], [3, 68, 0])
        self.assertEqual(reading["windows"][-1]["resetAt"], 1794520800000)
        self.assertNotIn("resetGrants", reading)

    def test_fable_429_still_returns_usage_but_an_unrelated_429_remains_an_error(self):
        headers = Message()
        for name, value in {"anthropic-ratelimit-unified-5h-utilization": "0.1",
                            "anthropic-ratelimit-unified-7d-utilization": "0.5",
                            "anthropic-ratelimit-unified-7d_oi-utilization": "1", "retry-after": "60"}.items():
            headers[name] = value
        error = urllib.error.HTTPError("https://api.anthropic.com/v1/messages", 429, "limited", headers, None)
        with patch.object(server.urllib.request, "urlopen", side_effect=error):
            reading = server.claude_probe({"plan": "max"}, self.login)
        self.assertEqual([w["usedPercent"] for w in reading["windows"]], [10, 50, 100])
        with patch.object(server, "request", side_effect=server.UsageError("limited", headers={"retry-after": "60"})):
            with self.assertRaises(server.UsageError):
                server.claude_probe({"plan": "max"}, self.login)


class Z0neTests(unittest.TestCase):
    def test_the_key_comes_from_the_z0ne_group_only(self):
        self.assertEqual(server.z0ne_key(), "z0ne-key")
        self.assertIn("z0ne", server.BY_ID)

    def test_a_config_without_the_group_has_no_key(self):
        with patch.object(server, "PROXY_CONFIG", str(root / "missing.yaml")):
            self.assertIsNone(server.z0ne_key())
        (root / "other.yaml").write_text(CONFIG.replace("https://api.z0ne.ai/v1", "https://example.com/v1"))
        with patch.object(server, "PROXY_CONFIG", str(root / "other.yaml")):
            self.assertIsNone(server.z0ne_key())

    def test_the_balance_reading_uses_the_dashboard_numbers(self):
        raw = {"credit": {"maxIdr": 79773, "usedIdr": 1328, "netRemainingIdr": 78445}, "summary": {"windows": {"24h": {"spentIdr": 271}}}}
        with patch.object(server, "request", return_value=({}, raw)) as request:
            reading = server.fetch(server.BY_ID["z0ne"])
        self.assertEqual(request.call_args.args[1]["Authorization"], "Bearer z0ne-key")
        self.assertEqual(reading, {"kind": "balance", "currency": "IDR", "remaining": 78445, "used": 1328, "limit": 79773, "resetAt": None,
                                   "plan": None, "scope": "key", "detail": "Prepaid balance", "spentLast24h": 271})


if __name__ == "__main__":
    unittest.main()
