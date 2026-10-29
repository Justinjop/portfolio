"""Learn from the ALL book (every signal taken): which signal features relate to trade outcome.
Descriptive only; guideline changes still follow PROTOCOL.md checkpoints.
python study.py
"""
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "desk.sqlite"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT p.pnl, p.exit_reason, p.side, p.entry_time, p.exit_time, p.entry_price, p.exit_price, "
                       "p.stop, s.features, s.ai_status FROM positions p "
                       "JOIN signals s ON s.id=p.signal_id WHERE p.book='ALL' AND p.status='closed'").fetchall()
    n = len(rows)
    print(f"ALL book closed trades: {n}")
    if n < 10:
        return
    data = [(r["pnl"] * 10.0, json.loads(r["features"]), r) for r in rows]   # USDT on 1000 notional -> bp
    hold = sorted((r["exit_time"] - r["entry_time"]) / 3_600_000 for r in rows)
    gross = [r["side"] * (r["exit_price"] / r["entry_price"] - 1) * 1e4 for r in rows]
    print(f"holding hours: median {hold[n // 2]:.1f}, p25 {hold[n // 4]:.1f}, p75 {hold[3 * n // 4]:.1f}; "
          f"<=2h: {sum(h <= 2 for h in hold)}/{n}")
    print(f"price move before costs: mean {sum(gross) / n:+.1f}bp, win {sum(g > 0 for g in gross) / n:.0%} "
          f"(costs ~20bp round trip)")
    avg = sum(d[0] for d in data) / n
    print(f"mean {avg:+.1f}bp | win {sum(d[0] > 0 for d in data) / n:.0%}")
    keys = ["age_candles", "atr_pct", "rank_by_volume", "dist_kijun_atr", "move_since_signal_atr", "dist_cloud_atr",
            "cloud_thick_pct", "tk_spread_atr", "ret24h", "btc_ret24h"]
    print("\nfeature split at median (low half vs high half mean bp, n):")
    for k in keys:
        vals = sorted(d[1][k] for d in data)
        med = vals[n // 2]
        lo = [d[0] for d in data if d[1][k] < med]
        hi = [d[0] for d in data if d[1][k] >= med]
        if lo and hi:
            print(f"  {k:<22} median {med:+.4g}: low {sum(lo) / len(lo):+7.1f}bp (n={len(lo)}) | "
                  f"high {sum(hi) / len(hi):+7.1f}bp (n={len(hi)})")
    for label, cond in (("long", lambda d: d[2]["side"] > 0), ("short", lambda d: d[2]["side"] < 0),
                        ("AI would take", lambda d: d[2]["ai_status"] == "taken"),
                        ("AI skipped", lambda d: d[2]["ai_status"] == "skipped")):
        sub = [d[0] for d in data if cond(d)]
        if sub:
            print(f"  {label:<14} n={len(sub):<4} mean {sum(sub) / len(sub):+.1f}bp")
    reasons = {}
    for d in data:
        reasons.setdefault(d[2]["exit_reason"], []).append(d[0])
    print("\nby exit reason:", {k: (len(v), round(sum(v) / len(v), 1)) for k, v in reasons.items()})
    print("\nCaution: small samples; a median split on n<100 is noise-prone. Use only for hypotheses.")


if __name__ == "__main__":
    main()
