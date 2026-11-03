import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import yaml

from verify import management_key, request, verify


ROOT = Path(__file__).resolve().parent.parent
OLD = ROOT.parent / "cliproxyapi"
STATE = ROOT / ".local/migration.json"
IMAGE = "cliproxyapi-akbar:v8.0.53-panel-v1.27.2"
CONTAINERS = ["cli-proxy-api", "cliproxyapi-usage"]
UNIT_DIR = Path.home() / ".config/systemd/user"


def run(*command, timeout=60, cwd=ROOT):
    return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True, timeout=timeout).stdout


def record(status, **values):
    data = {"status": status, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **values}
    temporary = STATE.with_suffix(".pending.json")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, STATE)
    print(status, flush=True)


def containers():
    return json.loads(run("docker", "inspect", *CONTAINERS, timeout=15))


def compose(root, file):
    run("docker", "compose", "-p", "cliproxyapi", "-f", str(root / file),
        "up", "-d", "--no-build", "--pull", "never", "cli-proxy-api", "usage", timeout=90, cwd=root)


def wait_healthy():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            request("http://127.0.0.1:8317", "/healthz")
            return
        except (OSError, RuntimeError):
            time.sleep(0.5)
    raise RuntimeError("Proxy did not become healthy within 30 seconds")


def rewire(link, target):
    temporary = link.with_name(link.name + ".cliproxyapi-migration")
    if temporary.is_symlink():
        assert temporary.resolve() == target.resolve()
    else:
        os.symlink(target, temporary)
    os.replace(temporary, link)


def keepalive_links():
    targets = {"keepalive@.service", "keepalive-hourly@.timer", "keepalive-halfhourly@.timer"}
    links = {}
    for path in UNIT_DIR.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.resolve()
        if target.parent == OLD / "keepalive/systemd" and target.name in targets:
            assert (ROOT / "keepalive/systemd" / target.name).is_file()
            links[path] = target
    return links


def refresh_runtime():
    for name in ["auths", "logs", "keepalive/env", "keepalive/logs", "keepalive/state"]:
        source = OLD / name
        if source.exists():
            shutil.copytree(source, ROOT / name, dirs_exist_ok=True, symlinks=True)
    for path in (ROOT / "keepalive/env").glob("*.env"):
        path.write_text(path.read_text().replace(str(OLD), str(ROOT)))
        path.chmod(0o600)
    cfg = yaml.safe_load((OLD / "config.yaml").read_text())
    cfg["management"]["disable-control-panel"] = False
    cfg["management"]["disable-auto-update-panel"] = True
    path = ROOT / "config.yaml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False)
    (ROOT / "auths").chmod(0o700)
    for path in (ROOT / "auths/active").glob("*.json"):
        path.chmod(0o600)


def publish_management():
    for path in ["/v8/management", "/management.html"]:
        run("sudo", "-n", "tailscale", "serve", "--bg", "--yes", "--https=8444",
            "--set-path=" + path, "http://127.0.0.1:8317" + path, timeout=20)


def rollback(old_links, links_changed):
    result = subprocess.run(["docker", "inspect", "cli-proxy-api"], text=True, capture_output=True, timeout=15)
    if result.returncode == 0:
        proxy = json.loads(result.stdout)[0]
        if proxy["State"]["Running"]:
            print("Rollback stops:", proxy["Name"], proxy["Image"], flush=True)
            run("docker", "stop", "--time", "10", "cli-proxy-api", timeout=25)
    if (ROOT / "auths/active").exists():
        shutil.copytree(ROOT / "auths", OLD / "auths", dirs_exist_ok=True, symlinks=True)
    if links_changed:
        for link, target in old_links.items():
            rewire(Path(link), Path(target))
        run("systemctl", "--user", "daemon-reload", timeout=20)
    compose(OLD, "docker-compose.yml")
    wait_healthy()


def migrate():
    existing = containers()
    expected = {"cli-proxy-api": "cli-proxy-api", "cliproxyapi-usage": "usage"}
    if all(d["Config"]["Labels"].get("com.docker.compose.project.working_dir") == str(ROOT) for d in existing):
        for link, target in keepalive_links().items():
            rewire(link, ROOT / "keepalive/systemd" / target.name)
        run("systemctl", "--user", "daemon-reload", timeout=20)
        publish_management()
        verify("http://127.0.0.1:8317", ROOT / "config.yaml", management_key(), external=True)
        record("complete", root=str(ROOT), image=IMAGE)
        return
    for container in existing:
        name = container["Name"].lstrip("/")
        labels = container["Config"]["Labels"]
        assert labels["com.docker.compose.project"] == "cliproxyapi"
        assert labels["com.docker.compose.service"] == expected[name]
        assert labels["com.docker.compose.project.working_dir"] == str(OLD)
        assert container["State"]["Running"], name
        print("Authorized replacement:", name, container["Image"], flush=True)
    run("docker", "image", "inspect", IMAGE, timeout=15)
    active = run("systemctl", "--user", "list-units", "--type=service", "--state=running,activating",
                 "--no-legend", "--no-pager", "keepalive@*.service", timeout=15)
    assert not active.strip(), "A keepalive service is active; retry outside its scheduled window"
    assert 120 <= time.time() % 1800 <= 1650, "Retry away from the keepalive schedule"
    old_links = keepalive_links()
    record("prepared", old_images={d["Name"]: d["Image"] for d in existing})
    stopped = False
    links_changed = False
    try:
        run("docker", "inspect", "cli-proxy-api", "--format", "{{.Name}} {{.State.Status}}", timeout=15)
        stopped = True
        run("docker", "stop", "--time", "10", "cli-proxy-api", timeout=25)
        refresh_runtime()
        compose(ROOT, "compose.host.yml")
        wait_healthy()
        links_changed = True
        for link, target in old_links.items():
            rewire(link, ROOT / "keepalive/systemd" / target.name)
        run("systemctl", "--user", "daemon-reload", timeout=20)
        publish_management()
        verify("http://127.0.0.1:8317", ROOT / "config.yaml", management_key(), external=True, live=True, save=True)
        old_cfg = yaml.safe_load((OLD / "config.yaml").read_text())
        new_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        old_cfg["management"]["disable-control-panel"] = False
        old_cfg["management"]["disable-auto-update-panel"] = True
        assert old_cfg == new_cfg, "Production settings changed beyond panel enablement"
        for path in (ROOT / "auths/active").glob("*.json"):
            before = json.loads((OLD / "auths/active" / path.name).read_text())
            after = json.loads(path.read_text())
            for key in ["type", "priority", "disabled", "websockets", "label"]:
                assert before.get(key) == after.get(key), (path.name, key)
        record("complete", root=str(ROOT), image=IMAGE)
    except BaseException as error:
        print("Migration failed:", type(error).__name__, str(error), flush=True)
        if stopped:
            rollback(old_links, links_changed)
            record("rolled-back", error=type(error).__name__ + ": " + str(error))
        else:
            record("failed-before-cutover", error=type(error).__name__ + ": " + str(error))
        raise


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(RuntimeError("Migration deadline reached")))
    with (ROOT / ".local/migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        migrate()


if __name__ == "__main__":
    main()
