import argparse
import json

from cpa import ACTIVE, CLAUDE, CODEX, config, request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    settings = config()
    assert settings["oauth"]["auth-dir"] == "/data/auths/active"
    assert settings["routing"]["strategy"] == "fill-first"
    assert settings["routing"]["session-affinity"] is False
    assert settings["management"]["secret-key"], "the management key is missing"
    assert settings["management"]["allow-remote"] is True
    assert settings["management"]["disable-control-panel"] is False
    assert settings["management"]["disable-auto-update-panel"] is True
    print("PASS: fill-first routing without session affinity, management panel enabled")

    files = sorted(path.name for path in ACTIVE.glob("*.json"))
    assert set(files) == set(CLAUDE) | set(CODEX), "auths/active differs from accounts.json: " + ", ".join(sorted(set(files) ^ (set(CLAUDE) | set(CODEX))))
    claude = {name: json.loads((ACTIVE / name).read_text()) for name in CLAUDE}
    for name, account in CODEX.items():
        record = json.loads((ACTIVE / name).read_text())
        assert record["type"] == "codex", name
        assert record.get("websockets") is True, name
        assert record.get("priority") == account["priority"], name + " priority is " + repr(record.get("priority"))
    for name, record in claude.items():
        account = CLAUDE[name]
        assert record["type"] == "claude", name
        assert record.get("label") == account["label"], name + " label is " + repr(record.get("label"))
        assert record.get("priority") == account["priority"], name + " priority is " + repr(record.get("priority"))
        assert record.get("disabled") is False, name + " is disabled"
        if account.get("setup_token"):
            assert record.get("setup_token") is True, name + " is not a setup-token record"
        else:
            assert record.get("refresh_token"), name + " has no refresh token"
    accounts = [record["account_uuid"] for record in claude.values()]
    assert len(accounts) == len(set(accounts)), "two pool files hold the same Claude account"
    order = lambda pool: ", ".join(a["label"] for a in sorted(pool.values(), key=lambda a: -a["priority"]))
    print("PASS: Claude " + order(CLAUDE) + "; Codex " + order(CODEX))

    claude_models = [item["id"] for item in request("GET", "/v1/models", anthropic=True)["data"]]
    assert "claude-opus-5-5" in claude_models, claude_models
    openai_models = [item["id"] for item in request("GET", "/v1/models")["data"]]
    assert "gpt-6-luna" in openai_models, openai_models
    print("PASS: the model lists have claude-opus-5-5 and gpt-6-luna")

    if args.live:
        data = request("POST", "/v1/messages", {
            "model": "claude-opus-5-5", "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        }, anthropic=True)
        assert any("OK" in item.get("text", "") for item in data.get("content", [])), data
        print("PASS: live Claude response")
        data = request("POST", "/v1/responses", {"model": "gpt-6-luna", "input": "Reply with exactly OK.", "reasoning": {"effort": "none"}})
        assert data.get("status") == "completed", data
        print("PASS: live Codex response")


if __name__ == "__main__":
    main()
