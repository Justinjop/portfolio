"""Daily Signal Desk backup (desk-backup.timer, 02:30 UTC).

1. Consistent copies of every sqlite DB (sqlite3 backup API) + integrity_check.
2. tar.gz with the DBs, code, docs, Claude settings/skills and systemd units -> ~/backups/desk-YYYYMMDD.tar.gz,
   keeping the newest 14. Secrets are NOT included (~/.config/desk, phone.json) and neither are charts.
3. Off-site copy: the archive is posted to the alert Discord webhook together with a daily status line,
   which doubles as a heartbeat (no post on a given day = something is wrong with the VM).
Run: ~/venv/bin/python backup.py [--no-post]
"""
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from watchdog import conditions, webhook

HOME = Path.home()
QT = HOME / "qt"
OUT = HOME / "backups"
KEEP = 14
DBS = [QT / "desk/desk.sqlite", QT / "news/news.sqlite", QT / "news/news_tech.sqlite", QT / "news/skills.sqlite",
       QT / "auto/auto.sqlite", QT / "lab/lab.sqlite", QT / "lab_sealed/holdout.sqlite"]
GLOBS = [(QT, ["*.md", "*.sh", "*.py"]), (QT / "desk", ["*.py", "*.md"]), (QT / "desk/hist", ["*.py"]),
         (QT / "desk/.claude", ["settings.json", "skills/**/*"]), (QT / "news", ["*.py"]), (QT / "auto", ["*.py", "*.md", "policy.json", "reports/*.md"]),
         (QT / "ops", ["*.py", "*.md", "*.sh"]), (QT / "lab", ["*.py", "*.md", "reports/*.md"]),
         (QT / "lab_sealed", ["*.py"]),
         (HOME, ["errs.sh", "tmux-main.sh", ".tmux.conf"]), (Path("/etc/systemd/system"), ["desk-*"])]
SECRET_NAMES = {"phone.json", "news.json", "testnet.env", "claude.env", ".env"}


def post_file(path, text):
    boundary = uuid.uuid4().hex
    payload = json.dumps({"embeds": [{"title": "📦 每日備份", "description": text[:4000], "color": 0x5B8FF0,
                                      "footer": {"text": "Signal Desk 備份（不含金鑰）"}}]})
    body = io.BytesIO()
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
               f"Content-Type: application/json\r\n\r\n{payload}\r\n".encode())
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{path.name}\"\r\n"
               f"Content-Type: application/gzip\r\n\r\n".encode())
    body.write(path.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(webhook(), data=body.getvalue(), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "SignalDesk (local, 1.0)"})
    urllib.request.urlopen(req, timeout=60).read()


def auto_line():
    """One-line testnet auto-trading summary from auto.sqlite (no API calls)."""
    try:
        c = sqlite3.connect(f"file:{QT / 'auto/auto.sqlite'}?mode=ro", uri=True)
        eq = c.execute("SELECT equity FROM equity ORDER BY ts").fetchall()
        n_open = c.execute("SELECT COUNT(*) FROM trades WHERE status IN ('open','closing')").fetchone()[0]
        day = c.execute("SELECT COUNT(*), COALESCE(SUM(net),0) FROM trades WHERE status='closed' AND exit_time>?",
                        (int(time.time() * 1000) - 86_400_000,)).fetchone()
        ver = json.loads((QT / "auto/policy.json").read_text())["version"]
        c.close()
        if not eq:
            return ""
        return (f"\n測試網自動交易：權益 {eq[-1][0]:,.0f} USDT（開始以來 {eq[-1][0] / eq[0][0] - 1:+.2%}），"
                f"持倉 {n_open} 筆，24 小時平倉 {day[0]} 筆 {day[1]:+.1f}U，策略 v{ver}")
    except Exception as e:
        return f"\n測試網自動交易：讀取失敗（{e}）"


def main():
    OUT.mkdir(exist_ok=True)
    day = time.strftime("%Y%m%d", time.gmtime())
    dest = OUT / f"desk-{day}.tar.gz"
    info = []
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(dest, "w:gz") as tar:
        for db in DBS:
            if not db.exists():
                continue
            copy = Path(tmp) / db.name
            src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            dst = sqlite3.connect(copy)
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            src.close()
            dst.close()
            if ok != "ok":
                raise SystemExit(f"integrity_check failed for {db.name}: {ok}")
            tar.add(copy, arcname=f"db/{db.name}")
            info.append(f"{db.name} {copy.stat().st_size // 1024}KB")
        n = 0
        for base, pats in GLOBS:
            for pat in pats:
                for f in base.glob(pat):
                    if f.is_file() and f.name not in SECRET_NAMES and not f.name.startswith("desk.py.bak"):
                        tar.add(f, arcname=f"files/{f.relative_to(base.anchor)}")
                        n += 1
    with tarfile.open(dest) as t:                     # verify the archive reads back
        members = t.getnames()
    assert any(m == "db/desk.sqlite" for m in members), "desk.sqlite missing from archive"
    for old in sorted(OUT.glob("desk-*.tar.gz"))[:-KEEP]:
        old.unlink()
    size = dest.stat().st_size
    print(f"{time.strftime('%F %T', time.gmtime())} backup: {dest.name} {size // 1024}KB, {len(members)} entries")
    if "--no-post" in sys.argv:
        return
    issues = conditions(time.time())
    uptime = subprocess.run(["uptime", "-p"], capture_output=True, text=True).stdout.strip()
    status = "✅ 全部服務正常" if not issues else "⚠ 目前有異常：\n" + "\n".join(issues.values())
    text = f"{status}\n主機 {uptime}\n資料庫：{'、'.join(info)}\n程式與設定檔 {n} 個（不含金鑰）" + auto_line()
    if size < 9 * 1024 * 1024:
        post_file(dest, text)
    else:
        from watchdog import post
        post("📦 每日備份", text + f"\n⚠ 備份 {size // 1048576}MB 超過 Discord 上限，只存在 VM 的 ~/backups", 0xB7791F)


if __name__ == "__main__":
    main()
