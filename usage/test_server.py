import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

directory = tempfile.TemporaryDirectory(prefix="cliproxyapi-usage-test-")
root = Path(directory.name)
(root / "accounts.json").write_text(json.dumps([{"id": "codex-alt", "provider": "codex", "label": "Codex (Alt)", "file": "codex.json"}]))
os.environ.update(USAGE_ALLOWED_LOGIN="owner@example.com", USAGE_ACCOUNTS=str(root / "accounts.json"))

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

    def test_haiku_probe_reads_session_and_weekly_without_a_fable_row(self):
        headers = {"anthropic-ratelimit-unified-5h-utilization": "0.06", "anthropic-ratelimit-unified-7d-utilization": "0.01"}
        with patch.object(server, "request", return_value=(headers, {})) as request:
            reading = server.claude_probe({"plan": "max"}, self.login, "haiku")
        self.assertEqual(request.call_args.args[2]["model"], "claude-haiku-5-5")
        self.assertEqual([w["label"] for w in reading["windows"]], ["Session", "Weekly"])

    def test_a_fable_refresh_inside_the_floor_is_not_served_a_haiku_reading(self):
        store = server.Store()
        server.BY_ID["andrew"] = {"id": "andrew", "provider": "claude", "name": "Andrew", "manual": True}
        store.states["andrew"] = {"reading": None, "fetchedAt": None, "checkedAt": None, "refreshAfter": 0, "error": None}
        store.locks["andrew"] = server.threading.Lock()
        reading = {"kind": "quota", "windows": [{"id": "five_hour"}]}
        try:
            with patch.object(server, "fetch", return_value=reading) as fetch:
                store.get("andrew", True, "haiku")
                store.get("andrew", True, "haiku")
                store.get("andrew", True, "fable")
                store.get("andrew", True, "fable")
                store.get("andrew")
            self.assertEqual([c.args[1] for c in fetch.call_args_list], ["haiku", "fable"])
        finally:
            del server.BY_ID["andrew"]

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


if __name__ == "__main__":
    unittest.main()
