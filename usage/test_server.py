import json
import os
from pathlib import Path
import tempfile
import unittest
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
