import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from cpa import ACTIVE, CLAUDE, image


TARGETS = {a["id"].removeprefix("claude-"): name for name, a in CLAUDE.items() if not a.get("setup_token")}
CALLBACK = "http://localhost:54545/callback"


def login():
    # The login runs in a throwaway container, so the new record never enters auths/active until adopt() writes it.
    work = Path(tempfile.mkdtemp(prefix="cpa-claude-login-"))
    (work / "auths").mkdir()
    (work / "config.yaml").write_text("port: 18399\nauth-dir: /login/auths\n")
    process = subprocess.Popen(
        ["docker", "run", "--rm", "--name", "cpa-claude-login", "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
         "-v", f"{work}:/login", image(), "./CLIProxyAPI", "-config", "/login/config.yaml", "-claude-login", "-no-browser"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for line in process.stdout:
            if line.startswith("https://claude.ai/oauth/authorize"):
                return process, work, line.strip()
        raise SystemExit("The proxy printed no Claude login link.")
    except BaseException:
        process.kill()
        raise


def adopt(record, target):
    for other in CLAUDE:
        if other != target and (ACTIVE / other).exists():
            assert record["account_uuid"] != json.loads((ACTIVE / other).read_text())["account_uuid"], \
                record.get("email", "This account") + " is already " + CLAUDE[other]["label"]
    if (ACTIVE / target).exists():
        record["priority"] = json.loads((ACTIVE / target).read_text()).get("priority", 0)
    record.update(label=CLAUDE[target]["label"], disabled=False)
    # Rewrite in place: the proxy reloads on WRITE but misses renames.
    descriptor = os.open(ACTIVE / target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(record, handle)
    return record.get("email", "")


def main():
    parser = argparse.ArgumentParser(description="Log a Claude account in to the proxy. Run as the user that owns auths/active, not root.")
    parser.add_argument("target", choices=TARGETS)
    target = TARGETS[parser.parse_args().target]
    if os.geteuid() == 0:
        raise SystemExit("Run without sudo: the proxy runs as your user and must own every auth file.")

    process, work, url = login()
    try:
        print("Open this link in a browser signed in to the account for " + CLAUDE[target]["label"] + ", and approve:\n")
        print(url + "\n")
        print("The browser then fails to load a localhost:54545/callback page. Copy that URL.")
        callback = getpass.getpass("Paste the callback URL (hidden): ").strip()
        if not callback.startswith(CALLBACK + "?"):
            raise SystemExit("That is not a " + CALLBACK + " URL.")
        urllib.request.urlopen(callback.replace("localhost", "127.0.0.1", 1), timeout=30).read()
        process.wait(timeout=90)
        created = list((work / "auths").glob("*.json"))
        assert len(created) == 1, "expected one new login file, found " + repr([p.name for p in created])
        email = adopt(json.loads(created[0].read_text()), target)
    finally:
        if process.poll() is None:
            subprocess.run(["docker", "rm", "-f", "cpa-claude-login"], capture_output=True)
        subprocess.run(["rm", "-rf", str(work)], check=False)

    print("Saved " + email + " as " + CLAUDE[target]["label"] + " in " + target)
    time.sleep(3)
    subprocess.run([sys.executable, str(Path(__file__).with_name("verify-claude.py"))], check=True)


if __name__ == "__main__":
    main()
