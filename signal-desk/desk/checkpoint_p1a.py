"""Pre-registered test H-P1A (study_notes.md, 2026-10-29): are P1 grade-A picks no better than non-picks?

Only signals registered after 2026-10-29 09:00 UTC (the hypothesis was formed on earlier data) and at least 72h
ago, so every ALLX trade has finished (time stop) - counting only the closed ones of recent signals would keep the
early stop-outs and drop the winners still running. Day-clustered bootstrap of (mean A - mean non-pick), bp after costs.
python checkpoint_p1a.py
"""
import random
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "desk.sqlite"
SINCE = 1_793_264_400_000          # 2026-10-29 09:00 UTC
MIN_N, MIN_DAYS = 30, 7
DAY = 86_400_000


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT p.pnl, p.qty, p.entry_price, s.pick_grade g, s.registered_at reg FROM positions p "
                       "JOIN signals s ON s.id=p.signal_id WHERE p.book='ALLX' AND p.status='closed' "
                       "AND s.registered_at>=? AND s.registered_at<=?",
                       (SINCE, int(time.time() * 1000) - 72 * 3_600_000)).fetchall()
    by_day = {}
    for r in rows:
        grp = "A" if r["g"] == "A" else "N" if r["g"] == "-" else None
        if grp:
            by_day.setdefault(r["reg"] // DAY, {"A": [], "N": []})[grp].append(1e4 * r["pnl"] / (r["qty"] * r["entry_price"]))
    a = [x for d in by_day.values() for x in d["A"]]
    n = [x for d in by_day.values() for x in d["N"]]
    days = [d for d in by_day.values() if d["A"] or d["N"]]
    print(f"H-P1A：{len(days)} 個訊號日；A 級 n={len(a)}，未推薦 n={len(n)}（只算 2026-10-29 09:00 UTC 之後、72 小時以前登記的 ALLX）")
    if len(a) < MIN_N or len(n) < MIN_N or len(days) < MIN_DAYS:
        print(f"樣本不足（需要兩組各 ≥{MIN_N} 筆、≥{MIN_DAYS} 個訊號日）：維持 P1，下一個檢查點再看。")
        return
    diff = sum(a) / len(a) - sum(n) / len(n)
    rng, boots = random.Random(0), []
    for _ in range(4000):
        pick = [rng.choice(days) for _ in days]
        aa = [x for d in pick for x in d["A"]]
        nn = [x for d in pick for x in d["N"]]
        if aa and nn:
            boots.append(sum(aa) / len(aa) - sum(nn) / len(nn))
    boots.sort()
    lo, hi = boots[int(len(boots) * .025)], boots[int(len(boots) * .975)]
    print(f"A 級平均 {sum(a) / len(a):+.1f}bp，未推薦 {sum(n) / len(n):+.1f}bp，差 {diff:+.1f}bp，日群集 CI95 [{lo:+.1f}, {hi:+.1f}]")
    if diff <= 0 and lo <= 0:
        print("結論：支持 H-P1A（A 級不比未推薦好）。檢查點可以考慮 P2。")
    elif lo > 0:
        print("結論：否定 H-P1A（A 級明顯較好）。維持 P1。")
    else:
        print("結論：證據不足，維持 P1。")


if __name__ == "__main__":
    main()
