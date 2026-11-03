import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

import yaml


ROOT = Path(__file__).resolve().parent.parent
PANEL_SHA256 = "2f6cf57a8ca4ace1fadde0280cbd41084f56999738d0e692a30b135420f7fc30"
TAILNET = "https://proxy-host.example.ts.net:8444"


def management_key():
    if os.environ.get("CPA_MANAGEMENT_KEY"):
        return os.environ["CPA_MANAGEMENT_KEY"]
    return (ROOT / ".local/management-key").read_text().strip()


def request(base, path, key=None, method="GET", payload=None, headers=None, expected=200):
    extra = {"User-Agent": "Mozilla/5.0 CLIProxyAPI-verifier", **(headers or {})}
    if key:
        extra["Authorization"] = "Bearer " + key
    if payload is not None and not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
        extra["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=payload, headers=extra, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status, body = response.code, response.read()
    if status != expected:
        raise RuntimeError(f"{method} {path} returned {status}, expected {expected}")
    return body


def verify_keepalive():
    units = Path.home() / ".config/systemd/user"
    source = ROOT / "keepalive/systemd"
    for path in units.rglob("*"):
        if path.name.startswith("keepalive") and path.is_symlink():
            assert path.resolve().is_relative_to(source), str(path)
    for name in ["claude-main", "claude-alt", "claude-fallback", "codex-main", "codex-alt"]:
        env = ROOT / "keepalive/env" / (name + ".env")
        assert env.is_file() and str(ROOT.parent / "cliproxyapi") + "/" not in env.read_text(), name
        timer = ("keepalive-halfhourly@" if name.startswith("codex-") else "keepalive-hourly@") + name + ".timer"
        state = subprocess.check_output(["systemctl", "--user", "is-active", timer], text=True, timeout=10).strip()
        assert state == "active", timer
        path = subprocess.check_output(["systemctl", "--user", "show", timer, "-p", "FragmentPath", "--value"],
                                       text=True, timeout=10).strip()
        assert Path(path).resolve().is_relative_to(source), timer
    print("PASS: all keepalive timers, enabled links, and environment paths use the fork", flush=True)


def verify(base, config_path, key, external=False, live=False, save=False):
    cfg = yaml.safe_load(config_path.read_text())
    client = cfg["access"]["api-keys"][0]
    assert cfg["routing"]["strategy"] == "fill-first"
    assert cfg["routing"]["session-affinity"] is False
    assert cfg["management"]["allow-remote"] is True
    assert cfg["management"]["disable-control-panel"] is False
    assert cfg["management"]["disable-auto-update-panel"] is True
    request(base, "/healthz")
    models = json.loads(request(base, "/v1/models", client))["data"]
    ids = {model["id"] for model in models}
    assert {"gpt-6.1-sol", "claude-opus-5-5", "juxta/deepseek-v4.1-flash"} <= ids
    print("PASS: health, client authentication, model discovery, and routing settings", flush=True)
    panel = request(base, "/management.html")
    assert hashlib.sha256(panel).hexdigest() == PANEL_SHA256
    print("PASS: original Management Center v1.27.2 checksum", flush=True)
    for path in ["/v0/management/auth-files", "/v8/management/credentials"]:
        data = json.loads(request(base, path, key))
        assert isinstance(data["files"], list)
    request(base, "/v8/management/credentials", expected=401)
    request(base, "/v8/management/credentials", key)
    print("PASS: v0 and v8 management authentication", flush=True)
    if save:
        before = yaml.safe_load(config_path.read_text())
        raw = request(base, "/v8/management/config.yaml", key)
        request(base, "/v8/management/config.yaml", key, "PUT", raw,
                {"Content-Type": "application/yaml"})
        assert yaml.safe_load(config_path.read_text()) == before
        print("PASS: management configuration save preserves settings", flush=True)
    if live:
        claude = json.loads(request(base, "/v1/messages", client, "POST", {
            "model": "claude-opus-5-5", "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        }, {"anthropic-version": "2023-06-01", "User-Agent": "claude-cli/2.1.316 (external, cli)"}))
        assert any("OK" in item.get("text", "") for item in claude.get("content", []))
        codex = json.loads(request(base, "/v1/responses", client, "POST", {
            "model": "gpt-6-luna", "input": "Reply with exactly OK.",
            "reasoning": {"effort": "none"},
        }))
        assert codex.get("status") == "completed"
        print("PASS: live Claude and Codex responses", flush=True)
    if external:
        verify_keepalive()
        for path in ["/v0/management/auth-files", "/v8/management/credentials", "/management.html"]:
            request("https://llm.example.com", path, expected=404)
        request("https://llm.example.com", "/v1/models", client)
        for path in ["/v0/management/auth-files", "/v8/management/credentials", "/management.html"]:
            request(TAILNET, path, key)
        usage = json.loads(request("https://proxy-host.example.ts.net:8443", "/v1/usage"))
        assert len(usage["accounts"]) == 5
        print("PASS: public API, public management blocks, private panel, T3 hub, and usage service", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8317")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--external", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--save-check", action="store_true")
    args = parser.parse_args()
    verify(args.base_url, args.config, management_key(), args.external, args.live, args.save_check)


if __name__ == "__main__":
    main()
