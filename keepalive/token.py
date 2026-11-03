import json
import os
import sys
import time
from datetime import datetime, timezone


def load(path):
    with open(path) as f:
        return json.load(f)


def check_expiry(login):
    expired = login.get("expired")
    if expired and datetime.fromisoformat(expired).timestamp() < time.time() + 120:
        sys.exit(f"access token expired at {expired}; the proxy has not refreshed it")


def claude(path):
    login = load(path)
    if "env" in login:
        print(login["env"]["CLAUDE_CODE_OAUTH_TOKEN"])
        return
    check_expiry(login)
    print(login.get("access_token") or login["setup_token"])


def codex(path, home):
    login = load(path)
    check_expiry(login)
    # refresh_token stays empty: the proxy owns the refresh chain, and a CLI refresh would spend it.
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": login.get("id_token", ""),
            "access_token": login["access_token"],
            "refresh_token": "",
            "account_id": login["account_id"],
        },
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    os.makedirs(home, mode=0o700, exist_ok=True)
    tmp = os.path.join(home, "auth.json.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(auth, f)
    os.replace(tmp, os.path.join(home, "auth.json"))


if __name__ == "__main__":
    if sys.argv[1] == "claude":
        claude(sys.argv[2])
    else:
        codex(sys.argv[2], sys.argv[3])
