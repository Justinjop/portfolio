"""Self-repair, called by watchdog.py when a new alert appears.

Step 1, fixed playbook (deterministic, done by this script, only operational actions):
  failed service      -> reset-failed + start it once again (--no-block; the next watchdog run sees the result)
  timer not active    -> enable --now the timer
  timer stale         -> start the service once
  backup stale        -> start desk-backup once
  disk > 85%          -> vacuum the systemd journal to 200 MB
  everything else     -> no action (reported only)
  Each action runs at most once per alert key per 6h.
Step 2, diagnosis: a read-only `claude -p` reads logs/code and explains the root cause and a proposed fix.
  It cannot edit files or run anything except read-only inspection commands; code changes stay with the user.
"""
import json
import subprocess
import time
from pathlib import Path

HOME = Path.home()
QT = HOME / "qt"
CLAUDE = HOME / ".local" / "bin" / "claude"
REPORTS = QT / "ops" / "repair_reports"
ALLOWED = ["Read(//home/ubuntu/qt/**)", "Bash(journalctl:*)", "Bash(systemctl status:*)", "Bash(systemctl list-timers:*)",
           "Bash(tail:*)", "Bash(free:*)", "Bash(df:*)", "Bash(ps:*)", "Bash(bash /home/ubuntu/errs.sh:*)",
           "Bash(/home/ubuntu/venv/bin/python -c \"import desk\")"]
DENIED = ["Read(//home/ubuntu/.config/**)", "Read(//home/ubuntu/qt/desk/phone.json)", "Read(//home/ubuntu/qt/lab_sealed/**)",
          "Edit", "Write", "WebFetch", "WebSearch", "Bash(curl:*)", "Bash(wget:*)", "Bash(sudo:*)", "Bash(rm:*)"]
UNITS = {"desk-hourly", "desk-review", "desk-news", "desk-news-tech", "desk-skills", "desk-backup", "desk-research", "desk-auto", "desk-autotune"}


def sudo(*args):
    r = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-150:]


def playbook(keys, state, now):
    done = state.setdefault("repair_done", {})
    actions = []
    for key in keys:
        if now - done.get(key, 0) < 6 * 3600:
            actions.append(f"• `{key}`：6 小時內已處理過，這次不重複動作")
            continue
        kind, _, unit = key.partition(":")
        ok, msg = None, ""
        if kind == "failed" and unit in UNITS:
            sudo("systemctl", "reset-failed", f"{unit}.service")
            ok, msg = sudo("systemctl", "start", "--no-block", f"{unit}.service")
            what = f"重新執行 `{unit}` 一次"
        elif kind == "timer-inactive" and unit in UNITS:
            ok, msg = sudo("systemctl", "enable", "--now", f"{unit}.timer")
            what = f"重新啟用 `{unit}.timer`"
        elif kind == "stale" and unit in UNITS:
            ok, msg = sudo("systemctl", "start", "--no-block", f"{unit}.service")
            what = f"手動補跑 `{unit}` 一次"
        elif kind == "backup-stale":
            ok, msg = sudo("systemctl", "start", "--no-block", "desk-backup.service")
            what = "手動補跑備份"
        elif kind == "disk":
            ok, msg = sudo("journalctl", "--vacuum-size=200M")
            what = "清理 systemd 日誌到 200MB"
        else:
            actions.append(f"• `{key}`：沒有安全的自動處理方式，只回報")
            continue
        done[key] = now
        actions.append(f"• {what}：{'已送出' if ok else '失敗 ' + msg}")
    return actions


def diagnose(alert_text, actions):
    prompt = (
        "你是這台 Signal Desk VM 的值班工程師。請先讀 /home/ubuntu/qt/CLAUDE.md。監控剛剛發出以下警報，"
        "自動處理程序也已經做了下面的動作。\n\n<alert>\n" + alert_text + "\n</alert>\n<actions>\n" + "\n".join(actions) +
        "\n</actions>\n\n請用唯讀的方式調查：用 journalctl -u <unit> -n 50 --no-pager、tail -n 50 <log>、"
        "systemctl status <unit> --no-pager、bash /home/ubuntu/errs.sh 90，以及讀相關程式碼。"
        "每次只下一個指令，不要用 cd、&&、管線。你不能也不應該修改任何檔案。\n"
        "輸出用繁體中文，總長不超過 1200 字，固定四段：\n"
        "**狀況**（一句話）\n**根本原因**（有證據就引用 log 行；不確定就說不確定）\n"
        "**自動處理是否足夠**（重跑能不能解決，還是會再壞）\n"
        "**建議使用者做的事**（如果要改程式，寫出檔案、位置和具體修改；沒有就寫「不需要」）\n"
        "不得輸出任何 webhook 網址、token 或金鑰。")
    r = subprocess.run([str(CLAUDE), "-p", prompt, "--permission-mode", "dontAsk",
                        "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED],
                       cwd=QT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
    out = r.stdout.strip()
    if r.returncode != 0 or len(out) < 30:
        return f"（AI 診斷失敗：claude rc={r.returncode}；{r.stderr.strip()[-150:]}）"
    return out


def run(keys, alert_text, state, now):
    REPORTS.mkdir(exist_ok=True)
    actions = playbook(keys, state, now) if keys else ["• 只有事件型警報，沒有可自動處理的項目"]
    try:
        diag = diagnose(alert_text, actions)
    except Exception as e:
        diag = f"（AI 診斷失敗：{e!r}）"[:300]
    report = "**已自動處理**\n" + "\n".join(actions) + "\n\n" + diag
    (REPORTS / f"{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}.md").write_text(alert_text + "\n\n---\n" + report)
    return report
