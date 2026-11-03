import json
import re
import urllib.request
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
ACTIVE = ROOT / "auths/active"
BASE_URL = "http://127.0.0.1:8317"
MAIN = "claude-akbar.json"
ALT = "claude-akbar-alt.json"
FALLBACK = "claude-fallback.json"
POOL = {MAIN: ("Claude (Main)", 50), ALT: ("Claude (Alt)", 100), FALLBACK: ("Claude (Fallback)", 10)}


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
