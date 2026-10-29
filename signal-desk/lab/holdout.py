"""Sealed-holdout check for one lab test (run only by the research orchestrator, never by the agent).

Budget: one holdout check per 7 days. Each spec can be checked once. Verdicts (fixed before any check ran):
  PASS      sealed mean > 0, above the sealed baseline, and day-clustered 95% CI lower bound > 0
  方向一致  sealed mean > 0 and above the sealed baseline, but the CI includes 0
  FAIL      otherwise
A PASS only puts the rule on the list for the user's next checkpoint; it never changes live rules.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
LAB = Path.home() / "qt" / "lab"
sys.path.insert(0, str(LAB))
import lab  # noqa: E402

DB = HERE / "holdout.sqlite"
BUDGET_DAYS = 7


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS checks(test_id INTEGER PRIMARY KEY, ts REAL, spec TEXT, result TEXT, verdict TEXT)")
    return con


def budget_left():
    last = db().execute("SELECT MAX(ts) FROM checks").fetchone()[0]
    return last is None or time.time() - last >= BUDGET_DAYS * 86400


def check(test_id):
    con = db()
    if con.execute("SELECT 1 FROM checks WHERE test_id=?", (test_id,)).fetchone():
        raise SystemExit(f"test #{test_id} was already checked")
    spec_s = sqlite3.connect(lab.DB).execute("SELECT spec FROM tests WHERE id=?", (test_id,)).fetchone()[0]
    spec = json.loads(spec_s)
    d, base = lab.select(pd.read_pickle(HERE / "sealed.pkl"), spec)
    if len(d) < 30:
        r, verdict = {"n": int(len(d))}, "FAIL"
    else:
        boots, days = lab.day_boot(d)
        lo = float(pd.Series(boots).quantile(0.025))
        hi = float(pd.Series(boots).quantile(0.975))
        mean, bmean = float(d.net_bp.mean()), float(base.net_bp.mean())
        r = {"n": int(len(d)), "days": days, "mean_bp": round(mean, 1), "ci95": [round(lo, 1), round(hi, 1)],
             "base_mean_bp": round(bmean, 1), "win": round(float((d.net_bp > 0).mean()), 3),
             "by_universe": {u: round(float(g.net_bp.mean()), 1) for u, g in d.groupby("universe", observed=True)}}
        verdict = ("PASS" if mean > 0 and mean > bmean and lo > 0 else
                   "方向一致" if mean > 0 and mean > bmean else "FAIL")
    con.execute("INSERT INTO checks VALUES(?,?,?,?,?)", (test_id, time.time(), spec_s, json.dumps(r, ensure_ascii=False), verdict))
    con.commit()
    return r, verdict


if __name__ == "__main__":
    print(check(int(sys.argv[1])))
