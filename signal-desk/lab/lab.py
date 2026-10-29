"""Research lab: evaluate Ichimoku-signal filter hypotheses on the RESEARCH split only.

Data: data/research.pkl = historical replay rows (majors 2021-, alts 2024-) with signal time t < 2025-07-01.
Signals from 2025-07-01 on are sealed elsewhere and are never readable from here (see ~/qt/lab_sealed).
Each row is one signal x entry style x exit rule, with features known at signal time and the net result
(net_bp, after 0.10%/side costs and funding).

Commands (the research agent may only run these):
  python lab.py describe                      dataset, feature quantiles, baselines
  python lab.py history [N]                   previous tests (all nights)
  python lab.py test --name NAME --entry now|pullback --exit EXIT --universe majors|alts|both
                     [--filter "feature<=value" ...]
Filters may only use signal-time features: side, atr_pct, dist_kijun_atr, dist_cloud_atr, cloud_thick_pct,
tk_spread_atr, ret24h, hour, weekday, and (from 2026-10-29) btc_ret7d, btc_ret30d, btc_ret30d_side, btc_cloud,
funding_8h, funding_side. Operators: <= >= < > == !=.
At most MAX_PER_DAY new tests per UTC day; repeating an identical spec returns the stored result for free.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "research.pkl"
DB = HERE / "lab.sqlite"
FEATURES = ["side", "atr_pct", "dist_kijun_atr", "dist_cloud_atr", "cloud_thick_pct", "tk_spread_atr", "ret24h",
            "hour", "weekday",
            # added 2026-10-29 (user decision) by lab_sealed/add_features.py; all known at signal time
            "btc_ret7d", "btc_ret30d", "btc_ret30d_side", "btc_cloud", "funding_8h", "funding_side"]
EXITS = ["desk", "stop_time", "opposite", "tp3_sl2", "trail_kijun"]
MAX_PER_DAY = 6
FILTER = re.compile(r"^\s*(\w+)\s*(<=|>=|==|!=|<|>)\s*(-?[\d.]+(?:e-?\d+)?)\s*$")
OPS = {"<=": np.less_equal, ">=": np.greater_equal, "<": np.less, ">": np.greater, "==": np.equal, "!=": np.not_equal}
# candidate = worth one look at the sealed data (the orchestrator decides; never changes live rules)
MIN_N, MIN_DAYS, MIN_POS_PERIODS = 300, 150, 0.70


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY, ts REAL, name TEXT, spec TEXT, "
                "hash TEXT UNIQUE, result TEXT, candidate INTEGER)")
    return con


def load():
    return pd.read_pickle(DATA)


def select(df, spec):
    d = df[(df.entry == spec["entry"]) & (df.exit == spec["exit"])]
    if spec["universe"] != "both":
        d = d[d.universe == spec["universe"]]
    base = d
    for f in spec["filters"]:
        col, op, val = FILTER.match(f).groups()
        v = d[col].to_numpy()
        d = d[OPS[op](v, float(val)) & ~np.isnan(v.astype(float))]
    return d, base


def day_boot(d, reps=1000, seed=0):
    """Day-clustered bootstrap: trades on the same UTC day are resampled together (they are correlated)."""
    day = d.t.dt.floor("D")
    sums = d.groupby(day).net_bp.sum().to_numpy()
    cnts = d.groupby(day).net_bp.size().to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(sums), (reps, len(sums)))
    boots = sums[idx].sum(1) / cnts[idx].sum(1)
    return boots, len(sums)


def stats(d, base, n_tests):
    if len(d) < 30:
        return {"n": int(len(d)), "note": "too few trades (<30)"}
    boots, days = day_boot(d)
    se = float(boots.std())
    from statistics import NormalDist
    alpha = 0.05 / max(1, n_tests)                      # Bonferroni over every test ever run in this lab
    z = NormalDist().inv_cdf(1 - alpha / 2)
    mean = float(d.net_bp.mean())
    half = d.groupby(d.t.dt.year.astype(str) + "H" + ((d.t.dt.month > 6) + 1).astype(str)).net_bp.mean()
    top5 = float(d.net_bp.sort_values().tail(max(1, len(d) // 20)).sum() / d.net_bp.sum()) if d.net_bp.sum() > 0 else None
    r = {"n": int(len(d)), "days": days, "share_of_base": round(len(d) / max(1, len(base)), 3),
         "mean_bp": round(mean, 1), "median_bp": round(float(d.net_bp.median()), 1),
         "win": round(float((d.net_bp > 0).mean()), 3),
         "ci95": [round(float(np.percentile(boots, 2.5)), 1), round(float(np.percentile(boots, 97.5)), 1)],
         "adj_lower": round(mean - z * se, 1), "tests_so_far": n_tests,
         "base_mean_bp": round(float(base.net_bp.mean()), 1), "base_n": int(len(base)),
         "half_years": {k: round(float(v), 1) for k, v in half.items()},
         "pos_periods": round(float((half > 0).mean()), 2),
         "top5pct_share_of_profit": None if top5 is None else round(top5, 2),
         "by_universe": {u: round(float(g.net_bp.mean()), 1) for u, g in d.groupby("universe", observed=True)}}
    r["candidate"] = bool(r["n"] >= MIN_N and days >= MIN_DAYS and r["adj_lower"] > 0
                          and mean > r["base_mean_bp"] and r["pos_periods"] >= MIN_POS_PERIODS)
    return r


def cmd_describe(df):
    print(f"research rows {len(df)}, signals {len(df[(df.entry == 'now') & (df.exit == 'desk')])}, "
          f"t {df.t.min():%Y-%m-%d} .. {df.t.max():%Y-%m-%d} (sealed: >= 2025-07-01, not accessible)")
    base = df[(df.entry == "now") & (df.exit == "stop_time")]
    print("\nfeature quantiles (entry=now, exit=stop_time rows):")
    q = base[FEATURES].quantile([.05, .2, .4, .5, .6, .8, .95]).round(4)
    print(q.to_string())
    print("\nbaselines (mean net bp / n) by entry x exit x universe:")
    g = df.groupby(["entry", "exit", "universe"], observed=True).net_bp.agg(["mean", "size"]).round(1)
    print(g.to_string())


def cmd_history(n):
    rows = db().execute("SELECT id, datetime(ts,'unixepoch'), name, spec, result, candidate FROM tests "
                        "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    total = db().execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    print(f"{total} tests so far (newest first)")
    for i, ts, name, spec, res, cand in rows:
        r = json.loads(res)
        print(f"#{i} {ts} {'★' if cand else ' '} {name} | {spec} | n={r.get('n')} mean={r.get('mean_bp')} "
              f"base={r.get('base_mean_bp')} ci95={r.get('ci95')} adj_lower={r.get('adj_lower')} pos={r.get('pos_periods')}")


def cmd_test(df, a):
    for f in a.filter:
        m = FILTER.match(f)
        if not m or m.group(1) not in FEATURES:
            raise SystemExit(f"bad filter {f!r}: use 'feature<=value' with a feature from {FEATURES}")
    spec = {"entry": a.entry, "exit": a.exit, "universe": a.universe, "filters": sorted(f.replace(" ", "") for f in a.filter)}
    s = json.dumps(spec, sort_keys=True)
    h = hashlib.sha1(s.encode()).hexdigest()[:16]
    con = db()
    old = con.execute("SELECT id, result FROM tests WHERE hash=?", (h,)).fetchone()
    if old:
        print(f"already tested as #{old[0]} (not counted again):\n{old[1]}")
        return
    day0 = time.time() // 86400 * 86400
    used = con.execute("SELECT COUNT(*) FROM tests WHERE ts>=?", (day0,)).fetchone()[0]
    if used >= MAX_PER_DAY:
        raise SystemExit(f"daily budget used ({used}/{MAX_PER_DAY}); write up what you have")
    n_tests = con.execute("SELECT COUNT(*) FROM tests").fetchone()[0] + 1
    d, base = select(df, spec)
    r = stats(d, base, n_tests)
    cur = con.execute("INSERT INTO tests(ts,name,spec,hash,result,candidate) VALUES(?,?,?,?,?,?)",
                      (time.time(), a.name[:80], s, h, json.dumps(r, ensure_ascii=False), int(r.get("candidate", False))))
    con.commit()
    print(f"test #{cur.lastrowid} ({used + 1}/{MAX_PER_DAY} today)\n" + json.dumps(r, ensure_ascii=False, indent=1))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("describe")
    h = sub.add_parser("history")
    h.add_argument("n", nargs="?", type=int, default=30)
    t = sub.add_parser("test")
    t.add_argument("--name", required=True)
    t.add_argument("--entry", choices=["now", "pullback"], required=True)
    t.add_argument("--exit", choices=EXITS, required=True)
    t.add_argument("--universe", choices=["majors", "alts", "both"], required=True)
    t.add_argument("--filter", action="append", default=[])
    a = p.parse_args()
    if a.cmd == "history":
        return cmd_history(a.n)
    df = load()
    cmd_describe(df) if a.cmd == "describe" else cmd_test(df, a)


if __name__ == "__main__":
    main()
