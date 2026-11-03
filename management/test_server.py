import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import bcrypt

from management.server import API_PATH, Controller, ManagementServer


class FakeController:
    accounts = {"claude-main": object()}

    def __init__(self):
        self.calls = []
        self.enabled = True

    def snapshot(self):
        return {"accounts": [{"id": "claude-main", "enabled": self.enabled}]}

    def set_enabled(self, account, enabled):
        self.calls.append((account, enabled))
        self.enabled = enabled
        return self.snapshot()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="cpa-management-test-")
        cls.root = Path(cls.folder.name)
        cls.config = cls.root / "config.yaml"
        cls.hashed = bcrypt.hashpw(b"test-management-key", bcrypt.gensalt(rounds=4)).decode()
        cls.config.write_text("management:\n  secret-key: '" + cls.hashed + "'\n")
        cls.panel = cls.root / "management.html"
        cls.panel.write_text("<title>management test</title>")
        cls.controller = FakeController()
        cls.server = ManagementServer(("127.0.0.1", 0), cls.controller, cls.config, cls.panel)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.folder.cleanup()

    def request(self, method="GET", path=API_PATH, body=None, key="test-management-key"):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = "Bearer " + key
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_authentication_is_required_for_reads_and_writes(self):
        before = list(self.controller.calls)
        for key in [None, "wrong-key"]:
            self.assertEqual(self.request(key=key)[0], 401)
            self.assertEqual(self.request("PUT", API_PATH + "/claude-main", '{"enabled":false}', key)[0], 401)
        self.assertEqual(self.controller.calls, before)

    def test_only_named_accounts_and_boolean_changes_are_accepted(self):
        before = list(self.controller.calls)
        self.assertEqual(self.request("PUT", API_PATH + "/other.timer", '{"enabled":false}')[0], 404)
        for body in ['{"enabled":"false"}', '{"enabled":0}', '{"enabled":false,"unit":"other"}', '[]', '{']:
            self.assertEqual(self.request("PUT", API_PATH + "/claude-main", body)[0], 400)
        self.assertEqual(self.controller.calls, before)

    def test_changes_return_confirmed_state_and_do_not_cache(self):
        status, body, headers = self.request("PUT", API_PATH + "/claude-main", '{"enabled":false}')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["accounts"][0]["enabled"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.controller.calls[-1], ("claude-main", False))
        self.request("PUT", API_PATH + "/claude-main", '{"enabled":true}')

    def test_key_rotation_takes_effect_on_the_next_request(self):
        replacement = bcrypt.hashpw(b"replacement-key", bcrypt.gensalt(rounds=4)).decode()
        try:
            self.config.write_text("management:\n  secret-key: '" + replacement + "'\n")
            self.assertEqual(self.request()[0], 401)
            self.assertEqual(self.request(key="replacement-key")[0], 200)
        finally:
            self.config.write_text("management:\n  secret-key: '" + self.hashed + "'\n")

    def test_only_the_panel_is_served_without_authentication(self):
        self.assertEqual(self.request(path="/management.html", key=None)[0], 200)
        for path in ["/config.yaml", "/../config.yaml", "/healthz", API_PATH + "/unknown"]:
            self.assertEqual(self.request(path=path, key=None)[0], 404)


class SnapshotTests(unittest.TestCase):
    def test_current_host_has_five_real_accounts_and_calendar_schedules(self):
        snapshot = Controller().snapshot()
        self.assertEqual(len(snapshot["accounts"]), 5)
        for row in snapshot["accounts"]:
            self.assertTrue(row["available"], row["id"])
            self.assertEqual(row["interval_seconds"], 1800 if row["id"].startswith("codex-") else 3600)
            self.assertIsInstance(row["enabled"], bool)
            self.assertNotIn("detail", row)
            self.assertNotIn("token", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
