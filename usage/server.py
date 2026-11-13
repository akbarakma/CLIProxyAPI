import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

AUTH_DIR = os.environ.get("USAGE_AUTH_DIR", "/auths")
ALLOWED_LOGIN = os.environ["USAGE_ALLOWED_LOGIN"]
PORT = int(os.environ.get("USAGE_PORT", "8318"))
FRESH_FOR = 60
FORCE_FLOOR = 10
CODEX_UA = "codex_cli_rs/0.166.0"
PROXY_CONFIG = os.environ.get("USAGE_PROXY_CONFIG", "/config/config.yaml")
Z0NE_BASE_URL = "https://api.z0ne.ai/v1"

with open(os.environ.get("USAGE_ACCOUNTS", "/config/accounts.json")) as accounts_file:
    ACCOUNTS = [{**account, "name": account["label"]} for account in json.load(accounts_file)]


# PyYAML is not in the image, and the key must stay in config.yaml only, so read the one line it sits on.
def z0ne_key():
    try:
        with open(PROXY_CONFIG) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    for index, line in enumerate(lines):
        if line.strip().removeprefix("- ") == f'base-url: "{Z0NE_BASE_URL}"':
            item_indent = len(line) - len(line.lstrip())
            for following in lines[index + 1:]:
                stripped = following.strip()
                if stripped.startswith("- ") and len(following) - len(following.lstrip()) <= item_indent:
                    break
                if stripped.removeprefix("- ").startswith("api-key:"):
                    return stripped.removeprefix("- ").removeprefix("api-key:").strip().strip('"\'') or None
    return None


if z0ne_key():
    ACCOUNTS.append({"id": "z0ne", "provider": "z0ne", "name": "z0ne"})
BY_ID = {account["id"]: account for account in ACCOUNTS}
CLAUDE_WINDOWS = [
    ("five_hour", "Session", 18000000), ("seven_day", "Weekly", 604800000),
    ("seven_day_opus", "Weekly Opus", 604800000), ("seven_day_sonnet", "Weekly Sonnet", 604800000),
]


class UsageError(Exception):
    def __init__(self, message, retry_after=60, headers=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.headers = headers


def ms(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return int(value * 1000 if value < 10_000_000_000 else value)


def clamp(value):
    return max(0.0, min(100.0, float(value)))


def window_label(minutes):
    return {300: "Session", 1440: "Daily", 10080: "Weekly"}.get(minutes) or ("Monthly" if 40320 <= minutes <= 44640 else f"{round(minutes / 60)}h")


def load_login(account):
    with open(os.path.join(AUTH_DIR, account["file"])) as f:
        login = json.load(f)
    expired = login.get("expired")
    if expired and ms(expired) <= time.time() * 1000:
        raise UsageError("The proxy has not renewed this login. Check the cli-proxy-api container.", 300)
    return login


def request(url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 401:
            raise UsageError("The provider rejected this login. Renew it on the proxy host.", 300)
        if error.code == 403:
            raise UsageError("The provider denied usage access (403).", 300)
        if error.code == 429:
            retry = error.headers.get("retry-after")
            raise UsageError("The provider rate limit was reached. Try again later.", max(60, int(retry) if retry and retry.isdigit() else 300), error.headers)
        raise UsageError(f"The usage request failed ({error.code}).")
    except (urllib.error.URLError, TimeoutError):
        raise UsageError("The provider took too long. Try again later.")
    with response:
        return response.headers, json.load(response)


def fable_window(account, used, reset):
    window = {"id": "seven_day_overage_included", "label": "Fable", "usedPercent": clamp(used), "durationMs": 604800000, "resetAt": ms(reset)}
    expiry = ms(account.get("fable_included_until"))
    if expiry is not None:
        if expiry <= time.time() * 1000:
            return None
        window["expiresAt"] = expiry
    return window


def claude_reset_grants(raw):
    inventory = raw.get("cedar_ember")
    if not isinstance(inventory, dict) or inventory.get("eligible") is not True or not isinstance(inventory.get("grants"), list):
        return None
    grants = []
    for grant in inventory["grants"]:
        if not isinstance(grant, dict) or not isinstance(grant.get("id"), str):
            continue
        remaining = grant.get("resets_left")
        if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
            continue
        clears = grant.get("clears") or []
        if grant.get("gives_back_weekly") or "seven_day_overage_included" in clears and "seven_day" not in clears:
            label = "Fable reset"
        elif "five_hour" in clears and "seven_day" in clears:
            label = "Full reset"
        elif "seven_day" in clears:
            label = "Weekly reset"
        elif "five_hour" in clears:
            label = "Session reset"
        else:
            label = "Limit reset"
        grants.append({"id": grant["id"], "label": label, "remainingCount": remaining,
                       "startsAt": ms(grant.get("starts_at")), "expiresAt": ms(grant.get("ends_at")),
                       "paused": bool(grant.get("paused")), "usableNow": grant.get("usable_now") is True})
    return {"grants": grants, "fetchedAt": int(time.time() * 1000)}


def claude_usage(account, login):
    _, raw = request("https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1", {
        "Authorization": "Bearer " + login["access_token"], "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-cli/2.1.322 (external, cli)", "x-app": "cli", "Accept": "application/json"})
    windows = [{"id": key, "label": label, "usedPercent": clamp(raw[key]["utilization"]), "durationMs": duration, "resetAt": ms(raw[key].get("resets_at"))}
               for key, label, duration in CLAUDE_WINDOWS if isinstance(raw.get(key), dict) and raw[key].get("utilization") is not None]
    for limit in raw.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if isinstance(model, str) and model.casefold() == "fable" and limit.get("percent") is not None:
            window = fable_window(account, limit["percent"], limit.get("resets_at"))
            if window:
                windows.insert(min(2, len(windows)), window)
            break
    reading = {"kind": "quota", "plan": account.get("plan"), "windows": windows}
    grants = claude_reset_grants(raw)
    if grants is not None:
        reading["resetGrants"] = grants
    return reading


# A probe can open a session, so refresh stays manual. Only the Fable probe returns the Fable (7d_oi) header.
PROBES = {"fable": "claude-fable-5-2", "haiku": "claude-haiku-5-5"}
def claude_probe(account, login, probe="fable"):
    try:
        headers, _ = request("https://api.anthropic.com/v1/messages", {
            "Authorization": "Bearer " + login["access_token"], "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json", "User-Agent": "claude-cli/2.1.316 (external, cli)"},
            {"model": PROBES[probe], "max_tokens": 1, "messages": [{"role": "user", "content": "k"}],
             "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]})
    except UsageError as error:
        if error.headers is None:
            raise
        headers = error.headers
        if not any(headers.get(f"anthropic-ratelimit-unified-{prefix}-utilization") is not None for prefix in ("5h", "7d", "7d_oi")):
            raise
    windows = []
    for key, label, duration, prefix in (("five_hour", "Session", 18000000, "5h"), ("seven_day", "Weekly", 604800000, "7d")):
        used = headers.get(f"anthropic-ratelimit-unified-{prefix}-utilization")
        if used is not None:
            reset = headers.get(f"anthropic-ratelimit-unified-{prefix}-reset")
            windows.append({"id": key, "label": label, "usedPercent": clamp(float(used) * 100), "durationMs": duration, "resetAt": ms(int(reset)) if reset else None})
    used = headers.get("anthropic-ratelimit-unified-7d_oi-utilization")
    if used is not None:
        reset = headers.get("anthropic-ratelimit-unified-7d_oi-reset")
        window = fable_window(account, float(used) * 100, int(reset) if reset else None)
        if window:
            windows.append(window)
    return {"kind": "quota", "plan": account.get("plan"), "windows": windows}


def codex_usage(account, login):
    headers = {"Authorization": "Bearer " + login["access_token"], "ChatGPT-Account-Id": login["account_id"],
               "User-Agent": CODEX_UA, "originator": "codex_cli_rs", "Accept": "application/json"}
    _, raw = request("https://chatgpt.com/backend-api/wham/usage", headers)
    limits = raw.get("rate_limit") or {}
    windows = []
    for index, key in enumerate(("primary_window", "secondary_window")):
        window = limits.get(key)
        if window:
            seconds = window["limit_window_seconds"]
            windows.append({"id": f"window-{index}", "label": window_label(seconds / 60), "usedPercent": clamp(window["used_percent"]), "durationMs": seconds * 1000, "resetAt": ms(window.get("reset_at"))})
    reading = {"kind": "quota", "plan": raw.get("plan_type"), "windows": windows}
    try:
        _, credits = request("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", {**headers, "OpenAI-Beta": "codex-1", "originator": "Codex Desktop"})
        reading["resetCredits"] = {
            "availableCount": credits["available_count"],
            "expirations": sorted((ms(c.get("expires_at")) for c in credits["credits"] if c.get("status") == "available" and c.get("reset_type") == "codex_rate_limits"), key=lambda v: v if v is not None else float("inf")),
            "fetchedAt": int(time.time() * 1000),
        }
    except (UsageError, KeyError, TypeError):
        pass
    return reading


# z0ne has no public balance API; its dashboard reads the same numbers from this endpoint with the API key.
def z0ne_usage():
    key = z0ne_key()
    if not key:
        raise UsageError("The z0ne key is missing from config.yaml on the proxy host.", 300)
    _, raw = request("https://console.z0ne.ai/api/panel/usage", {
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "cli-proxy-usage"},
        {"page": 1, "limit": 1})
    credit = raw["credit"]
    total = credit.get("maxIdr")
    return {"kind": "balance", "currency": "IDR", "remaining": credit["netRemainingIdr"], "used": credit.get("usedIdr"),
            "limit": total if total and total > 0 else None, "resetAt": None, "plan": None, "scope": "key", "detail": "Prepaid balance",
            "spentLast24h": ((raw.get("summary") or {}).get("windows") or {}).get("24h", {}).get("spentIdr")}


def fetch(account, probe="fable"):
    if account["provider"] == "z0ne":
        return z0ne_usage()
    login = load_login(account)
    if account["provider"] == "codex":
        reading = codex_usage(account, login)
    elif account.get("manual"):
        reading = claude_probe(account, login, probe)
    else:
        reading = claude_usage(account, login)
    if not reading["windows"]:
        raise UsageError("The provider did not report usage windows.")
    return reading


class Store:
    def __init__(self):
        self.states = {a["id"]: {"reading": None, "fetchedAt": None, "checkedAt": None, "refreshAfter": 0, "error": None} for a in ACCOUNTS}
        self.locks = {a["id"]: threading.Lock() for a in ACCOUNTS}
        self.probes = {}

    def view(self, account_id):
        account = BY_ID[account_id]
        return {"id": account_id, "provider": account["provider"], "name": account["name"], "manualRefresh": bool(account.get("manual")), **self.states[account_id]}

    def get(self, account_id, force=False, probe="fable"):
        account = BY_ID[account_id]
        with self.locks[account_id]:
            state = self.states[account_id]
            now = time.time()
            if force:
                if state["checkedAt"] and now - state["checkedAt"] / 1000 < FORCE_FLOOR and self.probes.get(account_id, "fable") == probe:
                    return self.view(account_id)
            elif account.get("manual") or now * 1000 < state["refreshAfter"]:
                return self.view(account_id)
            self.probes[account_id] = probe
            try:
                reading = fetch(account, probe)
                stamp = int(time.time() * 1000)
                state.update(reading=reading, fetchedAt=stamp, checkedAt=stamp, refreshAfter=int((time.time() + FRESH_FOR) * 1000), error=None)
            except UsageError as error:
                state.update(checkedAt=int(time.time() * 1000), refreshAfter=int((time.time() + error.retry_after) * 1000), error=str(error))
            except Exception as error:
                state.update(checkedAt=int(time.time() * 1000), refreshAfter=int((time.time() + 60) * 1000), error="Usage is unavailable. Check the login on the proxy host.")
                print(f"{account_id}: {type(error).__name__}: {error}", flush=True)
            return self.view(account_id)


store = Store()
pool = ThreadPoolExecutor(max_workers=len(ACCOUNTS))


class Handler(BaseHTTPRequestHandler):
    def send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self, force):
        if self.headers.get("Tailscale-User-Login") != ALLOWED_LOGIN:
            return self.send(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        path = path.rstrip("/")
        probe = parse_qs(query).get("probe", ["fable"])[-1]
        if probe not in PROBES:
            return self.send(400, {"error": "unknown probe"})
        if path == "/v1/usage" and not force:
            return self.send(200, {"accounts": list(pool.map(store.get, BY_ID)), "generatedAt": int(time.time() * 1000)})
        if path.startswith("/v1/usage/") and path[len("/v1/usage/"):] in BY_ID:
            return self.send(200, store.get(path[len("/v1/usage/"):], force, probe))
        self.send(404, {"error": "not found"})

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
