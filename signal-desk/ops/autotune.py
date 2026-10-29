"""Daily risk / money-management tuning loop for the testnet auto-executor (desk-autotune.timer).

1. Run a restricted `claude -p` in ~/qt/auto: it may read the journal/policy/code, edit journal.md, and run
   auto.py status / report / whatif / policy / set. It cannot place or close orders, pause, or read keys.
2. Facts (equity, trades, policy changes) come from auto.sqlite, not from the agent's text.
3. Post the agent's report + facts to the research channel.
Run: ~/venv/bin/python autotune.py [--dry-run]
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from watchdog import post, redact
from research import research_webhook

HOME = Path.home()
AUTO = HOME / "qt" / "auto"
CLAUDE = HOME / ".local" / "bin" / "claude"
PROMPT = Path(__file__).resolve().parent / "autotune_prompt.md"
REPORTS = AUTO / "reports"
PY = "/home/ubuntu/venv/bin/python"
ALLOWED = ["Read(//home/ubuntu/qt/auto/journal.md)", "Read(//home/ubuntu/qt/auto/auto.py)",
           "Read(//home/ubuntu/qt/auto/policy.json)", "Read(//home/ubuntu/qt/desk/PICKS.md)",
           "Read(//home/ubuntu/qt/desk/study_notes.md)", "Read(//home/ubuntu/qt/lab/notebook.md)",
           "Edit(//home/ubuntu/qt/auto/journal.md)",
           f"Bash({PY} auto.py status)", f"Bash({PY} auto.py report:*)", f"Bash({PY} auto.py whatif:*)",
           f"Bash({PY} auto.py policy)", f"Bash({PY} auto.py set:*)"]
DENIED = ["Read(//home/ubuntu/.config/**)", "Read(//home/ubuntu/qt/desk/phone.json)", "Read(//home/ubuntu/qt/lab_sealed/**)",
          "Edit(//home/ubuntu/qt/auto/policy.json)", "Edit(//home/ubuntu/qt/auto/auto.py)", "Write", "WebFetch",
          "WebSearch", "Bash(curl:*)", "Bash(wget:*)", "Bash(sudo:*)", "Bash(rm:*)",
          f"Bash({PY} auto.py run:*)", f"Bash({PY} auto.py flatten:*)", f"Bash({PY} auto.py pause:*)",
          f"Bash({PY} auto.py resume:*)"]


def run_agent(dry=False):
    prompt = PROMPT.read_text() + f"\n\n現在時間：{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC"
    env = dict(os.environ, AUTO_DRYRUN="1") if dry else None     # dry run: `auto.py set` validates but doesn't apply
    r = subprocess.run([str(CLAUDE), "-p", prompt, "--permission-mode", "dontAsk",
                        "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED],
                       cwd=AUTO, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800, env=env)
    out = r.stdout.strip()
    if r.returncode != 0 or "TITLE:" not in out:
        raise RuntimeError(f"claude rc={r.returncode} {out[-200:]!r} {r.stderr.strip()[-200:]}")
    out = out[out.index("TITLE:"):]
    title, _, body = out.partition("\n")
    return title.replace("TITLE:", "").strip(), body.strip()


def facts(since):
    con = sqlite3.connect(AUTO / "auto.sqlite")
    con.row_factory = sqlite3.Row
    eq = con.execute("SELECT equity FROM equity ORDER BY ts").fetchall()
    day = con.execute("SELECT COUNT(*) n, COALESCE(SUM(net),0) net, COALESCE(SUM(r_mult),0) r FROM trades "
                      "WHERE status='closed' AND exit_time>=?", (since - 86_400_000,)).fetchone()
    tot = con.execute("SELECT COUNT(*) n, COALESCE(SUM(net),0) net, AVG(r_mult) r FROM trades WHERE status='closed'"
                      ).fetchone()
    opened = con.execute("SELECT COUNT(*) FROM trades WHERE status NOT IN ('skipped','failed','test') AND created_at>=?",
                         (since - 86_400_000,)).fetchone()[0]
    skipped = con.execute("SELECT COUNT(*) FROM trades WHERE status='skipped' AND created_at>=?",
                          (since - 86_400_000,)).fetchone()[0]
    changes = con.execute("SELECT version, kind, changes, reason FROM policy_log WHERE ts>=?", (since,)).fetchall()
    lines = []
    if eq:
        lines.append(f"權益 {eq[0][0]:,.0f} → {eq[-1][0]:,.0f} USDT（{eq[-1][0] / eq[0][0] - 1:+.2%}，自動交易開始以來）")
    lines.append(f"過去 24 小時：開倉 {opened}、略過 {skipped}、平倉 {day['n']} 筆，淨 {day['net']:+.2f}U（{day['r']:+.2f}R）")
    if tot["n"]:
        lines.append(f"累計平倉 {tot['n']} 筆，淨 {tot['net']:+.2f}U，平均 {tot['r']:+.2f}R")
    for c in changes:
        lines.append(f"🔧 策略 v{c['version']}（{'降低風險' if c['kind'].startswith('derisk') else '一般修改'}）："
                     f"{c['changes']} — {c['reason']}")
    if not changes:
        lines.append("今天沒有修改策略參數")
    return lines


def main():
    dry = "--dry-run" in sys.argv
    REPORTS.mkdir(exist_ok=True)
    start = int(time.time() * 1000)
    title, body = run_agent(dry)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    (REPORTS / f"{stamp}.md").write_text(f"# {title}\n\n{body}\n")
    text = body[:2800] + "\n\n**資料庫紀錄（自動產生）**\n" + "\n".join(facts(start - 3_600_000))
    if dry:
        print(title, "\n", text)
        return
    post(f"🤖 自動交易學習 {stamp}：{title}"[:250], redact(text), 0x2B9DD9, webhook_url=research_webhook())
    print(f"{time.strftime('%F %T', time.gmtime())} autotune: ok")


if __name__ == "__main__":
    main()
