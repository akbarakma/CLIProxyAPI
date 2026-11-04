import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

import bcrypt
import yaml


ROOT = Path(__file__).resolve().parent.parent
API_PATH = "/v8/management/keepalive"


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    interval_seconds: int
    timer: str
    service: str


ACCOUNTS_PATH = Path(os.environ.get("FORK_ACCOUNTS", ROOT / ".local/accounts.json"))
INTERVALS = {"hourly": 3600, "halfhourly": 1800}


def load_accounts(path=ACCOUNTS_PATH):
    return tuple(
        Account(a["id"], a["label"], INTERVALS[a["keepalive"]],
                f"keepalive-{a['keepalive']}@{a['id']}.timer", f"keepalive@{a['id']}.service")
        for a in json.loads(path.read_text())
    )


def timestamp(value):
    if not value or value in ["n/a", "@0"]:
        return None
    return datetime.fromtimestamp(float(value.removeprefix("@")), timezone.utc).isoformat()


class Controller:
    def __init__(self, root=ROOT, accounts=None):
        self.root = root
        self.accounts = {account.id: account for account in accounts or load_accounts()}
        self.lock = threading.Lock()

    def command(self, *args):
        return subprocess.check_output(
            ["systemctl", "--user", *args], text=True, timeout=10,
            stderr=subprocess.PIPE, env={**os.environ, "LC_ALL": "C"},
        )

    def snapshot(self):
        units = [unit for account in self.accounts.values() for unit in [account.timer, account.service]]
        output = self.command("show", "--timestamp=unix", "--property=Id,LoadState,ActiveState,"
                              "UnitFileState,NextElapseUSecRealtime,ExecMainStartTimestamp,"
                              "ExecMainExitTimestamp,Result", *units)
        states = {}
        for block in output.strip().split("\n\n"):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if "Id" in fields:
                states[fields["Id"]] = fields
        rows = []
        for account in self.accounts.values():
            timer = states.get(account.timer, {})
            service = states.get(account.service, {})
            available = timer.get("LoadState") == "loaded" and service.get("LoadState") == "loaded"
            state_path = self.root / "keepalive/state" / account.id / "state.json"
            try:
                saved = json.loads(state_path.read_text())
            except FileNotFoundError:
                saved = {}
            if not isinstance(saved, dict):
                raise ValueError("Invalid keepalive state file")
            running = service.get("ActiveState") in ["activating", "active", "deactivating"]
            started_at = timestamp(service.get("ExecMainStartTimestamp"))
            checked_at = saved.get("checked_at")
            last_result = saved.get("status", "unknown")
            if checked_at is not None:
                if not isinstance(checked_at, str) or datetime.fromisoformat(checked_at).tzinfo is None:
                    raise ValueError("Invalid keepalive result timestamp")
            if not running and started_at and (
                not checked_at or datetime.fromisoformat(checked_at) < datetime.fromisoformat(started_at)
            ):
                last_result = "skipped" if service.get("Result") == "success" else "error"
                checked_at = timestamp(service.get("ExecMainExitTimestamp")) or started_at
            rows.append({
                "id": account.id, "label": account.label,
                "interval_seconds": account.interval_seconds,
                "available": available,
                "enabled": timer.get("UnitFileState") == "enabled",
                "timer_active": timer.get("ActiveState") == "active",
                "running": running,
                "next_run_at": timestamp(timer.get("NextElapseUSecRealtime")),
                "started_at": started_at,
                "last_result": last_result,
                "checked_at": checked_at,
            })
        return {"updated_at": datetime.now(timezone.utc).isoformat(), "accounts": rows}

    def set_enabled(self, account_id, enabled):
        account = self.accounts[account_id]
        with self.lock:
            self.command("enable" if enabled else "disable", "--now", account.timer)
            result = self.snapshot()
            row = next(row for row in result["accounts"] if row["id"] == account_id)
            if row["enabled"] != enabled or row["timer_active"] != enabled:
                raise RuntimeError("Timer did not reach the requested state")
            return result


class ManagementServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller, config_path, panel_path):
        self.controller = controller
        self.config_path = config_path
        self.panel_path = panel_path
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        print(format % args, flush=True)

    def send(self, status, value, content_type="application/json"):
        body = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 1000:
            self.send(401, {"error": "Management key required"})
            return False
        config = yaml.safe_load(self.server.config_path.read_text())
        hashed = config["management"]["secret-key"].encode()
        key = auth[7:].encode()
        if len(key) > 72 or not bcrypt.checkpw(key, hashed):
            self.send(401, {"error": "Invalid management key"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == "/management.html":
                return self.send(200, self.server.panel_path.read_bytes(), "text/html; charset=utf-8")
            if path != API_PATH:
                return self.send(404, {"error": "Not found"})
            if self.authenticated():
                self.send(200, self.server.controller.snapshot())
        except (OSError, ValueError, KeyError, yaml.YAMLError, subprocess.SubprocessError) as error:
            print(type(error).__name__, "while reading keepalive status", flush=True)
            self.send(503, {"error": "Keepalive status unavailable"})

    def do_PUT(self):
        path = urlsplit(self.path).path
        prefix = API_PATH + "/"
        account = path.removeprefix(prefix)
        if not path.startswith(prefix) or account not in self.server.controller.accounts:
            return self.send(404, {"error": "Unknown keepalive account"})
        try:
            if not self.authenticated():
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "JSON required"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128 or self.headers.get("Transfer-Encoding"):
                return self.send(400, {"error": "Invalid request body"})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or set(data) != {"enabled"} or type(data["enabled"]) is not bool:
                return self.send(400, {"error": "enabled must be a boolean"})
            self.send(200, self.server.controller.set_enabled(account, data["enabled"]))
        except (ValueError, UnicodeError):
            self.send(400, {"error": "Invalid request body"})
        except (OSError, KeyError, RuntimeError, yaml.YAMLError, subprocess.SubprocessError) as error:
            print(type(error).__name__, "while changing keepalive schedule", flush=True)
            self.send(503, {"error": "Could not confirm the keepalive setting; refresh status"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8319)
    args = parser.parse_args()
    server = ManagementServer(("127.0.0.1", args.port), Controller(), ROOT / "config.yaml",
                              ROOT / ".local/management-panel/management.html")
    server.serve_forever(poll_interval=0.5)
