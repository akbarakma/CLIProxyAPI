import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


ROOT = Path(__file__).resolve().parent.parent


def build():
    lock = json.loads((ROOT / "management/upstream.json").read_text())
    work = ROOT / ".local/management-ui" / lock["commit"]
    if not work.exists():
        url = f'https://codeload.github.com/{lock["repository"]}/tar.gz/{lock["commit"]}'
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read()
        if hashlib.sha256(archive).hexdigest() != lock["archive_sha256"]:
            raise RuntimeError("Management Center source checksum mismatch")
        work.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            for member in source.getmembers():
                parts = Path(member.name).parts[1:]
                if parts:
                    member.name = str(Path(*parts))
                    source.extract(member, work, filter="data")
    for source in (ROOT / "management/ui").rglob("*"):
        if source.is_file():
            target = work / source.relative_to(ROOT / "management/ui")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    page = work / "src/features/dashboard/DashboardPage.tsx"
    text = page.read_text()
    if "import { KeepalivePanel }" not in text:
        text = "import { KeepalivePanel } from './components/KeepalivePanel';\n" + text
    anchor = "      <section className={styles.section} ref={trafficRef}>"
    if "<KeepalivePanel />" not in text:
        if text.count(anchor) != 1:
            raise RuntimeError("Dashboard insertion point changed")
        text = text.replace(anchor, "      <KeepalivePanel />\n\n" + anchor)
    page.write_text(text)
    cooldown_test = work / "tests/authFileCooldowns.test.ts"
    text = cooldown_test.read_text()
    before = "expect(available).toContain(i18n.t('auth_files.cooldown_reset_hint'));"
    after = 'expect(available).toContain(i18n.t(\'auth_files.cooldown_reset_hint\').replaceAll("\'", "&#x27;"));'
    if before in text:
        cooldown_test.write_text(text.replace(before, after))
    strings = json.loads((ROOT / "management/strings.json").read_text())
    routing_strings = json.loads((ROOT / "management/routing-strings.json").read_text())
    for path in (work / "src/i18n/locales").glob("*.json"):
        data = json.loads(path.read_text())
        data["keepalive"] = strings
        data["routing"].update(routing_strings[path.stem])
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    bun = ["npm", "exec", "--yes", f'--package=bun@{lock["bun"]}', "--", "bun"]
    subprocess.run(bun + ["install", "--frozen-lockfile"], cwd=work, check=True, timeout=300)
    subprocess.run(bun + ["run", "verify"], cwd=work, check=True, timeout=600,
                   env={**os.environ, "VERSION": lock["version"] + "-akbar-keepalive"})
    output = ROOT / ".local/management-panel"
    output.mkdir(exist_ok=True)
    artifact = (work / "dist/index.html").read_bytes()
    temporary = output / "management.html.tmp"
    temporary.write_bytes(artifact)
    temporary.replace(output / "management.html")
    manifest = {**lock, "panel_sha256": hashlib.sha256(artifact).hexdigest()}
    (output / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Built", output / "management.html", flush=True)


if __name__ == "__main__":
    build()
