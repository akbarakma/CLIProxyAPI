import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import migrate


class RollbackTests(unittest.TestCase):
    def test_rollback_preserves_the_latest_refresh_token(self):
        with tempfile.TemporaryDirectory(prefix="cliproxyapi-rollback-test-") as directory:
            root = Path(directory) / "CLIProxyAPI"
            old = Path(directory) / "cliproxyapi"
            for folder, token in [(old, "spent-token"), (root, "latest-token")]:
                auth = folder / "auths/active"
                auth.mkdir(parents=True)
                (auth / "codex.json").write_text(json.dumps({"refresh_token": token}))
            running = subprocess.CompletedProcess([], 0, json.dumps([
                {"Name": "/cli-proxy-api", "Image": "new-image", "State": {"Running": True}}
            ]), "")
            with patch.object(migrate, "ROOT", root), patch.object(migrate, "OLD", old), \
                    patch.object(migrate.subprocess, "run", return_value=running), \
                    patch.object(migrate, "run") as command, patch.object(migrate, "compose") as compose, \
                    patch.object(migrate, "wait_healthy"):
                migrate.rollback({}, False)
                command.assert_called_once_with("docker", "stop", "--time", "10", "cli-proxy-api", timeout=25)
                compose.assert_called_once_with(old, "docker-compose.yml")
            restored = json.loads((old / "auths/active/codex.json").read_text())
            self.assertEqual(restored["refresh_token"], "latest-token")

    def test_rollback_recovers_when_replacement_container_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="cliproxyapi-rollback-test-") as directory:
            root = Path(directory) / "CLIProxyAPI"
            missing = subprocess.CompletedProcess([], 1, "", "No such container")
            with patch.object(migrate, "ROOT", root), \
                    patch.object(migrate.subprocess, "run", return_value=missing), \
                    patch.object(migrate, "run") as command, patch.object(migrate, "compose") as compose, \
                    patch.object(migrate, "wait_healthy"):
                migrate.rollback({}, False)
                command.assert_not_called()
                compose.assert_called_once_with(migrate.OLD, "docker-compose.yml")


if __name__ == "__main__":
    unittest.main()
