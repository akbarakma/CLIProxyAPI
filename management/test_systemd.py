from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from management.server import Account, Controller


class TimerControlTests(unittest.TestCase):
    def test_switches_persist_and_leave_the_running_ping_alone(self):
        name = "cliproxyapi-keepalive-check-" + uuid.uuid4().hex
        units = Path.home() / ".config/systemd/user"
        timer = units / (name + "@check.timer")
        service = units / (name + "@check.service")
        account = Account("claude-main", "Test account", 3600, timer.name, service.name)
        with tempfile.TemporaryDirectory(prefix="cpa-timer-state-") as root:
            controller = Controller(Path(root), (account,))
            source_timer = Path(root) / (name + "@.timer")
            source_service = Path(root) / (name + "@.service")
            links = [(units / source_timer.name, source_timer),
                     (units / source_service.name, source_service),
                     (timer, source_timer), (service, source_service)]
            try:
                source_timer.write_text("[Timer]\nOnCalendar=*-*-* 23:59:59\nUnit=" + service.name +
                                        "\n[Install]\nWantedBy=timers.target\n")
                source_service.write_text("[Service]\nType=oneshot\nExecStart=/usr/bin/sleep 30\n")
                for path, source in links:
                    self.assertFalse(path.exists() or path.is_symlink())
                    path.symlink_to(source)
                controller.command("daemon-reload")
                for ignored in range(2):
                    row = controller.set_enabled(account.id, True)["accounts"][0]
                    self.assertTrue(row["enabled"] and row["timer_active"])
                    self.assertIsNotNone(row["next_run_at"])
                subprocess.run(["systemctl", "--user", "start", "--no-block", service.name],
                               check=True, timeout=5)
                row = controller.set_enabled(account.id, False)["accounts"][0]
                self.assertFalse(row["enabled"] or row["timer_active"])
                self.assertTrue(row["available"])
                self.assertTrue(row["running"])
                print(controller.command("show", timer.name, "-p", "UnitFileState,ActiveState").strip())
                controller.command("daemon-reload")
                row = controller.snapshot()["accounts"][0]
                self.assertFalse(row["enabled"] or row["timer_active"])
                self.assertTrue(row["running"])
                row = controller.set_enabled(account.id, False)["accounts"][0]
                self.assertFalse(row["enabled"] or row["timer_active"])
            finally:
                for path in [timer, service]:
                    print("Cleaning test unit", path.name, flush=True)
                    print(controller.command("show", path.name, "-p", "Id,FragmentPath").strip(), flush=True)
                    controller.command("stop", path.name)
                    state = controller.command("show", path.name, "-p", "ActiveState", "--value").strip()
                    if state == "failed":
                        controller.command("reset-failed", path.name)
                controller.command("disable", timer.name)
                for path, source in links:
                    if path.is_symlink():
                        print("Removing test link", path, "to", path.resolve(), flush=True)
                        self.assertEqual(path.resolve(), source)
                        path.unlink()
                controller.command("daemon-reload")
                remaining = controller.command("list-units", "--all", "--plain", "--no-legend",
                                               timer.name, service.name)
                self.assertEqual(remaining.strip(), "")


if __name__ == "__main__":
    unittest.main()
