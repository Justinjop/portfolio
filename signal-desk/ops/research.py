"""Nightly research loop (desk-research.timer). Orchestrates; the agent never touches the sealed data.

1. Run a restricted `claude -p` in ~/qt/lab: it may only read the notebook/docs, edit notebook.md, and run lab.py.
2. Collect tonight's tests from lab.sqlite (facts come from the DB, not from the agent's text).
3. If a test is a ★candidate and the weekly budget allows, check the best one ONCE on the sealed holdout
   and append the verdict to the notebook (so later nights know, and the leak is counted and visible).
4. Post the agent's report + the facts table + any holdout verdict to Discord.
Run: ~/venv/bin/python research.py [--dry-run]
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from watchdog import post, redact

HOME = Path.home()
LAB = HOME / "qt" / "lab"
SEALED = HOME / "qt" / "lab_sealed"
CLAUDE = HOME / ".local" / "bin" / "claude"
PROMPT = Path(__file__).resolve().parent / "research_prompt.md"
REPORTS = LAB / "reports"
PY = "/home/ubuntu/venv/bin/python"
ALLOWED = ["Read(//home/ubuntu/qt/lab/notebook.md)", "Read(//home/ubuntu/qt/lab/lab.py)",
           "Read(//home/ubuntu/qt/desk/study_notes.md)", "Read(//home/ubuntu/qt/desk/PICKS.md)",
           "Read(//home/ubuntu/qt/desk/PROTOCOL.md)", "Edit(//home/ubuntu/qt/lab/notebook.md)",
           f"Bash({PY} lab.py describe)", f"Bash({PY} lab.py history:*)", f"Bash({PY} lab.py test:*)"]
DENIED = ["Read(//home/ubuntu/qt/lab_sealed/**)", "Read(//home/ubuntu/.config/**)", "Read(//home/ubuntu/qt/desk/phone.json)",
          "Write", "WebFetch", "WebSearch", "Bash(curl:*)", "Bash(wget:*)"]


def research_webhook():
    try:
        w = json.loads((HOME / ".config/desk/news.json").read_text()).get("research_webhook")
    except Exception:
        w = None
    return w


def run_agent():
    prompt = PROMPT.read_text() + f"\n\n現在時間：{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC"
    r = subprocess.run([str(CLAUDE), "-p", prompt, "--permission-mode", "dontAsk",
                        "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED],
                       cwd=LAB, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2400)
    out = r.stdout.strip()
    if r.returncode != 0 or "TITLE:" not in out:
        raise RuntimeError(f"claude rc={r.returncode} {out[-200:]!r} {r.stderr.strip()[-200:]}")
    out = out[out.index("TITLE:"):]
    title, _, body = out.partition("\n")
    return title.replace("TITLE:", "").strip(), body.strip()


def tonight(since_id):
    con = sqlite3.connect(LAB / "lab.sqlite")
    return [(i, n, json.loads(s), json.loads(r), c) for i, n, s, r, c in
            con.execute("SELECT id, name, spec, result, candidate FROM tests WHERE id>? ORDER BY id", (since_id,))]


def fmt_spec(s):
    return f"{s['universe']}/{s['entry']}/{s['exit']} " + (" ".join(s["filters"]) or "（無篩選）")


def main():
    dry = "--dry-run" in sys.argv
    REPORTS.mkdir(exist_ok=True)
    con = sqlite3.connect(LAB / "lab.sqlite")
    con.execute("CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY, ts REAL, name TEXT, spec TEXT, "
                "hash TEXT UNIQUE, result TEXT, candidate INTEGER)")
    since = con.execute("SELECT COALESCE(MAX(id),0) FROM tests").fetchone()[0]
    con.close()
    title, body = run_agent()
    tests = tonight(since)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    (REPORTS / f"{stamp}.md").write_text(f"# {title}\n\n{body}\n")

    facts = []
    for i, name, spec, r, cand in tests:
        if "mean_bp" not in r:
            facts.append(f"#{i} {name}：n={r.get('n')}，樣本不足")
            continue
        facts.append(f"#{i}{' ★' if cand else ''} {name}\n`{fmt_spec(spec)}`\nn={r['n']} 平均 {r['mean_bp']:+}bp"
                     f"（基準 {r['base_mean_bp']:+}）CI95 {r['ci95']} 校正下界 {r['adj_lower']:+}")
    total = sqlite3.connect(LAB / "lab.sqlite").execute("SELECT COUNT(*) FROM tests").fetchone()[0]

    hold = ""
    sys.path.insert(0, str(SEALED))
    import holdout
    cands = sorted([t for t in tests if t[4]], key=lambda t: -t[3]["adj_lower"])
    if cands and holdout.budget_left() and not dry:
        i, name, spec, _, _ = cands[0]
        r, verdict = holdout.check(i)
        hold = (f"🔒 封存資料驗證 #{i} {name}：**{verdict}**\nn={r.get('n')} 平均 {r.get('mean_bp')}bp"
                f"（基準 {r.get('base_mean_bp')}）CI95 {r.get('ci95')}")
        nb = LAB / "notebook.md"
        s = nb.read_text()
        line = (f"- {stamp} 測試 #{i}（{fmt_spec(spec)}）：{verdict}；n={r.get('n')}，平均 {r.get('mean_bp')}bp，"
                f"基準 {r.get('base_mean_bp')}bp，CI95 {r.get('ci95')}，分市場 {r.get('by_universe')}\n")
        nb.write_text(s.replace("## 封存驗證紀錄\n", "## 封存驗證紀錄\n" + line, 1))
    elif cands:
        hold = "★ 有候選，但本週的封存驗證額度已經用掉（每 7 天 1 次），下週再驗證。"

    text = body[:2600] + "\n\n**資料庫紀錄（自動產生）**\n" + ("\n".join(facts) or "今晚沒有新的測試") + \
        f"\n累計測試 {total} 個（多重檢定校正用）" + (f"\n\n{hold}" if hold else "")
    if dry:
        print(title, "\n", text)
        return
    post(f"🔬 研究迴圈 {stamp}：{title}"[:250], redact(text), 0x7C5CBF, webhook_url=research_webhook())
    print(f"{time.strftime('%F %T', time.gmtime())} research: {len(tests)} tests, total {total}, holdout {'yes' if hold.startswith('🔒') else 'no'}")


if __name__ == "__main__":
    main()
