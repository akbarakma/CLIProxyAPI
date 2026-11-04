import json
import os
import re
import urllib.request
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
ACTIVE = ROOT / "auths/active"
BASE_URL = "http://127.0.0.1:8317"


def setting(name):
    if os.environ.get(name):
        return os.environ[name]
    for line in (ROOT / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"")
    raise SystemExit(name + " is not set in the environment or .env")


ACCOUNTS = json.loads(Path(os.environ.get("FORK_ACCOUNTS", ROOT / ".local/accounts.json")).read_text())
CLAUDE = {account["file"]: account for account in ACCOUNTS if account["provider"] == "claude"}
CODEX = {account["file"]: account for account in ACCOUNTS if account["provider"] == "codex"}


def config():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def image():
    return re.search(r"CLI_PROXY_IMAGE:-([^}]+)", (ROOT / "compose.host.yml").read_text()).group(1)


def client_key():
    return config()["access"]["api-keys"][0]


def request(method, path, payload=None, timeout=90, anthropic=False):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE_URL + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + client_key())
    req.add_header("Content-Type", "application/json")
    if anthropic:
        req.add_header("anthropic-version", "2023-06-01")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())
