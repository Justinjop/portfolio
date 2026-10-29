"""Signal Desk watchdog -> Discord alerts. Runs every 15 min (desk-watchdog.timer).

Conditions (alerted when they appear, repeated every 6h while they last, "recovered" when they clear):
  - a desk-* service ended in failure / a desk-* timer is not active
  - a timer has not fired within its expected period (hourly 75 min, daily 26h)
  - disk > 85%, available memory < 150 MB, swap > 80%
  - newest backup older than 26h
Events (alerted once):
  - new error rows in desk.sqlite `runs`
  - new suspicious lines in review.log / news.log (Claude failures, tracebacks, permission denials)
Webhook: news.json "alert_webhook" if present, else phone.json "discord_webhook". Never printed.
Run: ~/venv/bin/python watchdog.py [--dry-run]
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
QT = HOME / "qt"
STATE = QT / "watchdog_state.json"
DESK_DB = QT / "desk" / "desk.sqlite"
BACKUPS = HOME / "backups"
LOGS = {"review.log": QT / "review.log", "news.log": QT / "news" / "news.log", "auto.log": QT / "auto" / "auto.log",
        "autotune.log": QT / "ops" / "autotune.log"}
BAD_LINE = re.compile(r"AUTO-ERROR|claude rc=|Traceback|Invalid API key|authentication_error|OAuth|\b401\b|usage limit|"
                      r"rate.?limit|權限被拒|permission denied|沒有完成", re.I)
TIMERS = {"desk-hourly": 75 * 60, "desk-review": 75 * 60, "desk-news": 75 * 60, "desk-news-tech": 75 * 60,
          "desk-skills": 26 * 3600, "desk-backup": 26 * 3600, "desk-research": 26 * 3600,
          "desk-auto": 20 * 60, "desk-autotune": 26 * 3600}
REPEAT = 6 * 3600
MAX_REPAIRS_PER_DAY = 6


def redact(s):
    s = re.sub(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+", "[webhook]", s)
    return re.sub(r"sk-ant-[\w-]+", "[token]", s)


def webhook():
    try:
        w = json.loads((HOME / ".config/desk/news.json").read_text()).get("alert_webhook")
        if w:
            return w
    except Exception:
        pass
    return json.loads((QT / "desk/phone.json").read_text())["discord_webhook"]


def post(title, text, color, webhook_url=None):
    body = {"embeds": [{"title": title, "description": text[:4000], "color": color,
                        "footer": {"text": "Signal Desk 監控"}}]}
    req = urllib.request.Request(webhook_url or webhook(), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "SignalDesk (local, 1.0)"})
    urllib.request.urlopen(req, timeout=20).read()


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()


def show(unit, *props):
    out = sh("systemctl", "show", unit, "--timestamp=unix", *[f"-p{p}" for p in props])
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def conditions(now):
    found = {}
    for name, period in TIMERS.items():
        t = show(f"{name}.timer", "ActiveState", "LastTriggerUSec", "LoadState")
        if t.get("LoadState") != "loaded":
            continue                                  # not installed (yet)
        if t.get("ActiveState") != "active":
            found[f"timer-inactive:{name}"] = f"⏸ `{name}.timer` 沒有啟用（{t.get('ActiveState')}）"
        last = t.get("LastTriggerUSec", "")
        if last.startswith("@") and now - int(last[1:]) > period:
            found[f"stale:{name}"] = f"⌛ `{name}` 已經 {(now - int(last[1:])) / 3600:.1f} 小時沒有執行"
        s = show(f"{name}.service", "Result", "ActiveState")
        if s.get("ActiveState") == "failed" or s.get("Result", "success") != "success":
            tail = redact(sh("journalctl", "-u", f"{name}.service", "-n", "6", "--no-pager", "-o", "cat"))[-700:]
            found[f"failed:{name}"] = f"❌ `{name}` 上次執行失敗（{s.get('Result')}）\n```\n{tail or '（無 journal）'}\n```"
    pct = int(sh("df", "--output=pcent", "/").splitlines()[-1].strip(" %"))
    if pct > 85:
        found["disk"] = f"💾 磁碟使用 {pct}%"
    mem = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
    if mem["MemAvailable"] < 150 * 1024:
        found["mem"] = f"🧠 可用記憶體只剩 {mem['MemAvailable'] // 1024} MB"
    if mem["SwapTotal"] and (mem["SwapTotal"] - mem["SwapFree"]) / mem["SwapTotal"] > 0.8:
        found["swap"] = f"🧠 swap 使用超過 80%"
    backups = sorted(BACKUPS.glob("desk-*.tar.gz"))
    if backups and now - backups[-1].stat().st_mtime > 26 * 3600:
        found["backup-stale"] = f"📦 最新備份是 {(now - backups[-1].stat().st_mtime) / 3600:.0f} 小時前"
    return found


def events(st, now):
    out = []
    since = st.get("runs_ts", int((now - 900) * 1000))
    try:
        con = sqlite3.connect(f"file:{DESK_DB}?mode=ro", uri=True)
        rows = con.execute("SELECT ts, kind, substr(info,1,200) FROM runs WHERE ts>? AND kind LIKE '%error%' "
                           "ORDER BY ts", (since,)).fetchall()
        for ts, kind, info in rows[:8]:
            out.append(f"🐞 desk `{kind}`：{redact(info or '')}")
        if rows:
            st["runs_ts"] = rows[-1][0]
        else:
            st.setdefault("runs_ts", since)
    except Exception as e:
        out.append(f"🐞 讀不到 desk.sqlite：{e!r}"[:200])
    offs = st.setdefault("offsets", {})
    for name, path in LOGS.items():
        if not path.exists():
            continue
        size = path.stat().st_size
        start = offs.get(name, size)                  # first run: start from the end, no history alerts
        if start > size:
            start = 0                                 # log was rotated/truncated
        with path.open("rb") as f:
            f.seek(start)
            new = f.read(size - start).decode("utf-8", "replace")
        offs[name] = size
        bad = [l.strip() for l in new.splitlines() if BAD_LINE.search(l)]
        if bad:
            out.append(f"📝 `{name}` 有異常：\n" + "\n".join(f"> {redact(l)[:220]}" for l in bad[:5]))
    return out


def main():
    dry = "--dry-run" in sys.argv
    now = time.time()
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    active = st.setdefault("active", {})
    found = conditions(now)
    evs = events(st, now)
    new = [k for k in found if k not in active or now - active[k] > REPEAT]
    cleared = [k for k in active if k not in found]
    lines = [found[k] for k in new] + evs
    if dry:
        print("conditions:", found, "\nevents:", evs, "\nwould alert:", lines, "\ncleared:", cleared)
        return
    if lines:
        post("🚨 Signal Desk 警報", "\n\n".join(lines), 0xE5484D)
    if cleared:
        post("✅ Signal Desk 已恢復", "\n".join(f"• {k}" for k in cleared), 0x30A46C)
    for k in new:
        active[k] = now
    for k in cleared:
        del active[k]
    st["last_run"] = now
    STATE.write_text(json.dumps(st))          # save before the (slow) repair so a crash there can't re-alert
    runs = [t for t in st.get("repair_runs", []) if now - t < 86400]
    if lines and len(runs) < MAX_REPAIRS_PER_DAY and "--no-repair" not in sys.argv:
        import repair
        report = repair.run([k for k in new if k in found], "\n\n".join(lines), st, now)
        post("🔧 自我修復報告", redact(report), 0xD9822B)
        st["repair_runs"] = runs + [now]
        STATE.write_text(json.dumps(st))
    elif lines:
        post("🔧 自我修復暫停", f"今天已經執行 {len(runs)} 次自動修復（上限 {MAX_REPAIRS_PER_DAY}），這次只發警報。", 0x8A929C)
    print(f"{time.strftime('%F %T', time.gmtime(now))} watchdog: {len(found)} conditions, {len(evs)} events, "
          f"sent {len(lines)}, cleared {len(cleared)}")


if __name__ == "__main__":
    main()
