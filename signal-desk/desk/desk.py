"""Signal Desk (PROTOCOL.md): forward paper test of SignalScanner 1h signals with ALL / RAND / AI books.

python desk.py hourly                 scan, register new signals, open ALL/RAND, update every open position
python desk.py pending                list AI-pending signals (features + chart path)
python desk.py decide ID take|skip --conf 0.6 --note "..."
python desk.py review                 AI open positions with fresh charts
python desk.py close POS_ID --note "..."
python desk.py report
No API keys are used; public market data only.
"""
import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote   # some symbols are non-ASCII (e.g. 龙虾USDT)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import SignalScanner as S  # noqa: E402

DB = HERE / "desk.sqlite"
CHARTS = HERE / "charts"
NOTIONAL = 1000.0
COST_SIDE = 0.0010
ATR_MULT = 2.5
TIME_STOP_MS = 72 * 3_600_000
AI_MAX_OPEN, AI_MAX_SAME_SIDE = 10, 6
AI_EXPIRY_MS = 2 * 3_600_000
# Addendum 2026-10-27 (PROTOCOL.md): rule books without the kijun exit; RULE1 threshold fixed from 2021-2024 replay
NO_KIJUN_BOOKS = {"ALLX", "RULE1"}
RULE1_MAX_ATR = 0.0130
BOOKS = ("ALL", "RAND", "AI", "ALLX", "RULE1")
HOUR, FIVE = 3_600_000, 300_000


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY, key TEXT UNIQUE, symbol TEXT, side INTEGER, signal_time INTEGER,
        registered_at INTEGER, price REAL, features TEXT, chart TEXT, rand_pick INTEGER,
        ai_status TEXT, ai_conf REAL, ai_note TEXT, ai_version TEXT, decided_at INTEGER);
    CREATE TABLE IF NOT EXISTS positions(
        id INTEGER PRIMARY KEY, book TEXT, signal_id INTEGER, symbol TEXT, side INTEGER, qty REAL,
        entry_time INTEGER, entry_price REAL, atr REAL, stop REAL, status TEXT,
        exit_time INTEGER, exit_price REAL, exit_reason TEXT, fees REAL, funding REAL, pnl REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS runs(ts INTEGER, kind TEXT, info TEXT);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    for col, typ in (("user_pick", "INTEGER"), ("pick_rank", "INTEGER"), ("pick_grade", "TEXT"),
                     ("pick_version", "TEXT"), ("batch_size", "INTEGER")):
        if col not in cols:
            con.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
    return con


def guideline_version() -> str:
    first = (HERE / "guidelines.md").read_text(encoding="utf-8").splitlines()[0]
    return first.split("準則")[-1].split("(")[0].split("（")[0].strip() or "v?"


# ── market data ──
def klines(symbol: str, interval: str, limit: int = 300, start: int | None = None) -> list[list]:
    q = f"symbol={quote(symbol)}&interval={interval}&limit={limit}" + (f"&startTime={start}" if start else "")
    return S.get_json(f"{S.BASE_URL}/fapi/v1/klines?{q}")


def atr14(kl: list[list]) -> float:
    trs = []
    for i in range(1, len(kl)):
        h, l, pc = float(kl[i][2]), float(kl[i][3]), float(kl[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / min(14, len(trs))


def features(res, price: float, btc24: float, rank: int) -> dict:
    kl, ich, i = res.klines, res.ichimoku, res.signal_index
    side = 1 if res.signal == "漲" else -1
    last = len(kl) - 1
    a = atr14(kl[-60:])
    la, lb = ich.leading_a[last], ich.leading_b[last]
    top, bot = max(la, lb), min(la, lb)
    return {
        "age_candles": last - i, "atr_pct": a / price, "rank_by_volume": rank, "quote_volume": res.quote_volume,
        "dist_kijun_atr": side * (price - ich.kijun[last]) / a,
        "move_since_signal_atr": side * (price - float(kl[i][4])) / a,
        "dist_cloud_atr": side * (price - (top if side > 0 else bot)) / a,
        "cloud_thick_pct": (top - bot) / price, "tk_spread_atr": side * (ich.tenkan[last] - ich.kijun[last]) / a,
        "ret24h": price / float(kl[-25][4]) - 1, "btc_ret24h": btc24,
    }


# ── user picks (PICKS.md): every batch, all ATR <= RULE1_MAX_ATR (grade A) plus the lowest-ATR rest up to ceil(n/3) (B) ──
PICK_VERSION = "P1"
# stablecoin pairs and near-zero ATR: 0.2% round-trip cost would exceed any move, so never recommended (grade "x")
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "RLUSD", "PYUSD", "EUR", "XUSD"}
PICK_MIN_ATR = 0.002


def assign_picks(con, ids: list[int]):
    rows = con.execute(f"SELECT id, symbol, features FROM signals WHERE id IN ({','.join('?' * len(ids))})",
                       ids).fetchall()
    atr = {r["id"]: json.loads(r["features"])["atr_pct"] for r in rows}
    ok = [r for r in rows if r["symbol"][:-4] not in STABLE_BASES and atr[r["id"]] >= PICK_MIN_ATR]
    ranked = sorted(ok, key=lambda r: atr[r["id"]]) + [r for r in rows if r not in ok]
    need = math.ceil(len(ok) / 3)
    for rank, r in enumerate(ranked, 1):
        low = atr[r["id"]] <= RULE1_MAX_ATR
        pick = r in ok and (low or rank <= need)
        grade = "x" if r not in ok else "A" if low else ("B" if pick else "-")
        con.execute("UPDATE signals SET user_pick=?, pick_rank=?, pick_grade=?, pick_version=?, batch_size=? WHERE id=?",
                    (int(pick), rank, grade, PICK_VERSION, len(ranked), r["id"]))


def pick_desc(r) -> str:
    f = json.loads(r["features"])
    stop = r["price"] * (1 - r["side"] * ATR_MULT * f["atr_pct"])
    return (f"{r['pick_grade']} {r['symbol']} {'多' if r['side'] > 0 else '空'} 價 {r['price']:.6g} "
            f"ATR {f['atr_pct']:.2%} 參考停損 {stop:.6g}")


def send_ntfy(topic, title, rows):
    import urllib.parse
    import urllib.request
    body = {"topic": topic, "title": title, "message": "\n".join(pick_desc(r) for r in rows),
            "tags": ["chart_with_upwards_trend"]}
    urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                                  headers={"Content-Type": "application/json"}), timeout=10).read()
    for r in rows:   # one message per pick with its chart attached (ntfy.sh keeps attachments for a few hours)
        if not r["chart"] or not Path(r["chart"]).exists():
            continue
        q = urllib.parse.urlencode({"t": f"{r['symbol']} {'多' if r['side'] > 0 else '空'}", "m": pick_desc(r)})
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic}?{q}", data=Path(r["chart"]).read_bytes(), method="PUT",
            headers={"Filename": Path(r["chart"]).name}), timeout=30).read()


def send_discord(webhook, title, rows):
    """One webhook message per up-to-10 picks: an embed per pick with its chart attached (multipart upload)."""
    import urllib.request
    for i in range(0, len(rows), 10):
        chunk = rows[i:i + 10]
        embeds, files = [], []
        for r in chunk:
            e = {"title": f"{r['symbol']} {'做多' if r['side'] > 0 else '做空'}（{r['pick_grade']} 級）",
                 "description": pick_desc(r), "color": 0x0F8A5F if r["side"] > 0 else 0xC8372D}
            if r["chart"] and Path(r["chart"]).exists():
                name = Path(r["chart"]).name
                e["image"] = {"url": f"attachment://{name}"}
                files.append((name, Path(r["chart"]).read_bytes()))
            embeds.append(e)
        payload = {"content": f"**{title}**" if i == 0 else None, "embeds": embeds}
        boundary = "deskboundary" + str(now_ms())
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                 f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n"]
        for n, (name, data) in enumerate(files):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{n}]\"; filename=\"{name}\"\r\n"
                         f"Content-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        urllib.request.urlopen(urllib.request.Request(
            webhook, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                         "User-Agent": "SignalDesk (local, 1.0)"}), timeout=30).read()


def notify_picks(con, ids: list[int]):
    """Push each batch's picks to the channels set in phone.json (ntfy_topic, discord_webhook). Never fails the run."""
    cfg_path = HERE / "phone.json"
    if not cfg_path.exists():
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    rows = con.execute(f"SELECT symbol, side, pick_grade, price, features, chart FROM signals WHERE user_pick=1 "
                       f"AND id IN ({','.join('?' * len(ids))}) ORDER BY pick_rank", ids).fetchall()
    if not rows:
        return
    title = f"新推薦 {len(rows)} 個（共 {len(ids)} 個訊號）"
    for key, fn in (("ntfy_topic", send_ntfy), ("discord_webhook", send_discord)):
        if cfg.get(key):
            try:
                fn(cfg[key], title, rows)
            except Exception as err:
                con.execute("INSERT INTO runs VALUES(?,?,?)", (now_ms(), "notify_error", f"{key}: {err!r}"[:500]))


# ── positions ──
def open_position(con, book, sig, price, atr, note=None):
    side = sig["side"]
    entry = price
    qty = NOTIONAL / entry
    stop = entry - side * ATR_MULT * atr
    con.execute("INSERT INTO positions(book,signal_id,symbol,side,qty,entry_time,entry_price,atr,stop,status,fees,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)",
                (book, sig["id"], sig["symbol"], side, qty, now_ms(), entry, atr, stop, NOTIONAL * COST_SIDE, note))


def funding_paid(symbol, side, qty, t0, t1, fallback_px) -> float:
    rows = S.get_json(f"{S.BASE_URL}/fapi/v1/fundingRate?symbol={quote(symbol)}&startTime={t0}&limit=100")
    paid = 0.0
    for r in rows:
        ft = int(r["fundingTime"])
        if t0 < ft <= t1:
            mark = float(r.get("markPrice") or 0) or fallback_px
            paid += side * qty * mark * float(r["fundingRate"])
    return paid


def find_exit(p, server_time):
    """Earliest of: 5m stop touch, 1h close through kijun, opposite signal, 72h time stop."""
    side, e, stop = p["side"], p["entry_time"], p["stop"]
    best = None
    k5 = [k for k in klines(p["symbol"], "5m", 1000, start=e - e % FIVE) if int(k[6]) <= server_time]
    for k in k5:
        if int(k[6]) <= e:
            continue
        o, h, l = float(k[1]), float(k[2]), float(k[3])
        first = int(k[0]) <= e < int(k[6])            # entry bar: its whole range counts (conservative)
        if side > 0 and l <= stop:
            best = (int(k[0]) if not first else e, stop if first else min(stop, o), "stop")
            break
        if side < 0 and h >= stop:
            best = (int(k[0]) if not first else e, stop if first else max(stop, o), "stop")
            break
    kl = [k for k in klines(p["symbol"], "1h", 300) if int(k[6]) <= server_time]
    if len(kl) > S.MIN_KLINES:
        closed, sigs, ich = S.build_signal_history(kl, server_time)
        for j, k in enumerate(closed):
            ct = int(k[6])
            if ct <= e:
                continue
            if best and ct >= best[0]:
                break
            c = float(k[4])
            opp = sigs[j] is not None and (1 if sigs[j] == "漲" else -1) == -side
            if opp:
                return (ct, c, "opposite_signal")
            if p["book"] not in NO_KIJUN_BOOKS and math.isfinite(ich.kijun[j]) and side * (c - ich.kijun[j]) < 0:
                return (ct, c, "kijun_cross")
            if ct - e >= TIME_STOP_MS:
                return (ct, c, "time_stop")
    return best


def close_position(con, p, t, px, reason, note=None):
    fees = p["fees"] + p["qty"] * px * COST_SIDE
    fund = funding_paid(p["symbol"], p["side"], p["qty"], p["entry_time"], t, px)
    pnl = p["side"] * p["qty"] * (px - p["entry_price"]) - fees - fund
    con.execute("UPDATE positions SET status='closed', exit_time=?, exit_price=?, exit_reason=?, fees=?, funding=?, pnl=?,"
                " note=COALESCE(?, note) WHERE id=?", (t, px, reason, fees, fund, pnl, note, p["id"]))
    return pnl


def update_positions(con, server_time):
    n = 0
    for p in con.execute("SELECT * FROM positions WHERE status='open'").fetchall():
        try:
            ex = find_exit(p, server_time)
        except Exception as err:   # keep the position; retry next hour
            con.execute("INSERT INTO runs VALUES(?,?,?)", (now_ms(), "update_error", f"{p['id']} {err}"))
            continue
        if ex:
            close_position(con, p, *ex)
            n += 1
    return n


# ── commands ──
def hourly(_args):
    con = db()
    server_time = S.get_server_time()
    syms = S.get_symbols()
    vols = S.get_24h_quote_volumes()
    order = sorted((s for s in syms if s in vols), key=vols.__getitem__, reverse=True)
    # Same IP as bfbot: 499 klines cost weight 2 per request instead of 10 (1500), and no older-history
    # fetches. Enough for Ichimoku warm-up and for signals within the last 6 candles.
    S.KLINE_LIMIT, S.MAX_HISTORY_CANDLES = 499, 499
    summary = S.scan_symbols_by_volume(order, syms, vols, "1h")
    prices = S.get_current_prices()
    btc = [k for k in klines("BTCUSDT", "1h", 30) if int(k[6]) <= server_time]
    btc24 = float(btc[-1][4]) / float(btc[-25][4]) - 1
    new_ids = []
    CHARTS.mkdir(exist_ok=True)
    for res in summary.results:
        if res.quote_asset != "USDT":       # USDC duplicates of the same coin would double-count
            continue
        if not S.is_signal_recent_for_chart(res, server_time) or res.symbol not in prices:
            continue
        side = 1 if res.signal == "漲" else -1
        key = f"{res.symbol}|{res.signal_time}|{side}"
        if con.execute("SELECT 1 FROM signals WHERE key=?", (key,)).fetchone():
            continue
        price = prices[res.symbol]
        feats = features(res, price, btc24, order.index(res.symbol) + 1)
        try:
            tmp = S.render_signal_chart(res.symbol, res.klines, res.signal_index, res.signal, "1h",
                                        output_dir=CHARTS / "tmp", ichimoku=res.ichimoku)
            dst = CHARTS / f"{res.symbol}_{res.signal_time}_{'L' if side > 0 else 'S'}.png"
            Path(tmp).replace(dst)
            chart = str(dst)
        except Exception:
            chart = None
        pick = int(hashlib.sha256(key.encode()).hexdigest(), 16) % 3 == 0
        cur = con.execute("INSERT INTO signals(key,symbol,side,signal_time,registered_at,price,features,chart,rand_pick,"
                          "ai_status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                          (key, res.symbol, side, res.signal_time, now_ms(), price, json.dumps(feats), chart, int(pick)))
        sig = con.execute("SELECT * FROM signals WHERE id=?", (cur.lastrowid,)).fetchone()
        a = feats["atr_pct"] * price
        open_position(con, "ALL", sig, price, a)
        if pick:
            open_position(con, "RAND", sig, price, a)
        open_position(con, "ALLX", sig, price, a)
        if feats["atr_pct"] <= RULE1_MAX_ATR:
            open_position(con, "RULE1", sig, price, a)
        new_ids.append(sig["id"])
    new = len(new_ids)
    if new_ids:
        assign_picks(con, new_ids)
        notify_picks(con, new_ids)
    con.execute("UPDATE signals SET ai_status='expired' WHERE ai_status='pending' AND registered_at < ?",
                (now_ms() - AI_EXPIRY_MS,))
    closed = update_positions(con, S.get_server_time())
    con.execute("INSERT INTO runs VALUES(?,?,?)", (now_ms(), "hourly", json.dumps(
        {"scanned": summary.scanned_count, "signals_visible": len(summary.results), "new": new, "closed": closed})))
    con.commit()
    manual = "-"
    try:   # the user's manual trades on the futures testnet (read-only), see testnet_sync.py
        import testnet_sync
        manual = testnet_sync.sync(con)
    except Exception as err:
        con.execute("INSERT INTO runs VALUES(?,?,?)", (now_ms(), "testnet_sync_error", repr(err)[:500]))
        con.commit()
    print(f"{iso(now_ms())} hourly: scanned {summary.scanned_count}, new signals {new}, positions closed {closed}, "
          f"manual trades {manual}")


def pending(_args):
    con = db()
    rows = con.execute("SELECT * FROM signals WHERE ai_status='pending' ORDER BY registered_at").fetchall()
    open_ai = con.execute("SELECT side FROM positions WHERE book='AI' AND status='open'").fetchall()
    print(f"AI open {len(open_ai)}/{AI_MAX_OPEN} (long {sum(r['side'] > 0 for r in open_ai)}, "
          f"short {sum(r['side'] < 0 for r in open_ai)}); pending {len(rows)}")
    for r in rows:
        f = json.loads(r["features"])
        print(f"[{r['id']}] {r['symbol']} {'LONG' if r['side'] > 0 else 'SHORT'} signal {iso(r['signal_time'])} "
              f"age {f['age_candles']} | ATR {f['atr_pct']:.2%} kijun {f['dist_kijun_atr']:+.1f}ATR "
              f"cloud {f['dist_cloud_atr']:+.1f}ATR moved {f['move_since_signal_atr']:+.1f}ATR thick {f['cloud_thick_pct']:.2%} "
              f"24h {f['ret24h']:+.1%} BTC24h {f['btc_ret24h']:+.1%} volRank {f['rank_by_volume']}\n    chart: {r['chart']}")


def decide(args):
    con = db()
    sig = con.execute("SELECT * FROM signals WHERE id=?", (args.id,)).fetchone()
    if not sig or sig["ai_status"] != "pending":
        raise SystemExit(f"signal {args.id} is not pending")
    ver = guideline_version()
    if args.action == "take":
        open_ai = con.execute("SELECT side FROM positions WHERE book='AI' AND status='open'").fetchall()
        if len(open_ai) >= AI_MAX_OPEN:
            raise SystemExit("AI book is full")
        if sum(r["side"] == sig["side"] for r in open_ai) >= AI_MAX_SAME_SIDE:
            raise SystemExit("same-side limit reached")
        price = S.get_current_prices()[sig["symbol"]]
        a = json.loads(sig["features"])["atr_pct"] * sig["price"]
        open_position(con, "AI", sig, price, a, note=args.note)
    con.execute("UPDATE signals SET ai_status=?, ai_conf=?, ai_note=?, ai_version=?, decided_at=? WHERE id=?",
                ("taken" if args.action == "take" else "skipped", args.conf, args.note, ver, now_ms(), args.id))
    con.commit()
    print(f"signal {args.id}: {args.action} ({ver})")


def review(_args):
    con = db()
    server_time = S.get_server_time()
    prices = S.get_current_prices()
    rows = con.execute("SELECT p.*, s.signal_time FROM positions p JOIN signals s ON s.id=p.signal_id "
                       "WHERE p.book='AI' AND p.status='open'").fetchall()
    for p in rows:
        kl = [k for k in klines(p["symbol"], "1h", 300) if int(k[6]) <= server_time]
        closed, sigs, ich = S.build_signal_history(kl, server_time)
        idx = next((j for j, k in enumerate(closed) if int(k[0]) == p["signal_time"]), len(closed) - 1)
        try:
            tmp = S.render_signal_chart(p["symbol"], closed, idx, "漲" if p["side"] > 0 else "跌", "1h",
                                        output_dir=CHARTS / "tmp", ichimoku=ich)
            chart = CHARTS / "review" / f"pos{p['id']}_{p['symbol']}.png"
            chart.parent.mkdir(parents=True, exist_ok=True)
            Path(tmp).replace(chart)
        except Exception as err:
            chart = f"render failed: {err}"
        px = prices[p["symbol"]]
        upnl = p["side"] * p["qty"] * (px - p["entry_price"]) - p["fees"]
        print(f"[pos {p['id']}] {p['symbol']} {'LONG' if p['side'] > 0 else 'SHORT'} since {iso(p['entry_time'])} "
              f"entry {p['entry_price']:.6g} now {px:.6g} stop {p['stop']:.6g} uPnL {upnl:+.2f} "
              f"({upnl / NOTIONAL:+.2%}) held {(now_ms() - p['entry_time']) / HOUR:.1f}h\n    chart: {chart}")


def close_cmd(args):
    con = db()
    p = con.execute("SELECT * FROM positions WHERE id=? AND book='AI' AND status='open'", (args.id,)).fetchone()
    if not p:
        raise SystemExit("not an open AI position")
    px = S.get_current_prices()[p["symbol"]]
    pnl = close_position(con, p, now_ms(), px, "ai_discretionary", note=args.note)
    con.commit()
    print(f"closed pos {args.id} at {px:.6g}, pnl {pnl:+.2f}")


def _stats(rets: list[float], rng=random.Random(0)) -> str:
    if not rets:
        return "n=0"
    n, m = len(rets), sum(rets) / len(rets)
    boots = sorted(sum(rng.choice(rets) for _ in range(n)) / n for _ in range(2000))
    wins = [r for r in rets if r > 0]
    loss = -sum(r for r in rets if r <= 0)
    pf = sum(wins) / loss if loss > 0 else float("inf")
    return (f"n={n} mean {m * 1e4:+.1f}bp [95% {boots[50] * 1e4:+.1f}, {boots[1949] * 1e4:+.1f}] "
            f"win {len(wins) / n:.0%} pf {pf:.2f} total {sum(rets) * NOTIONAL:+.2f} USDT")


def report(_args):
    con = db()
    try:
        prices = S.get_current_prices()
    except Exception:
        prices = {}
    for book in BOOKS:
        rows = con.execute("SELECT pnl, qty, entry_price, stop FROM positions WHERE book=? AND status='closed'",
                           (book,)).fetchall()
        rets = [r["pnl"] / NOTIONAL for r in rows]
        opn = con.execute("SELECT symbol, side, qty, entry_price, fees FROM positions WHERE book=? AND status='open'",
                          (book,)).fetchall()
        n_open = len(opn)
        print(f"{book:<5} closed {_stats(rets)} | open {n_open}")
        # closed-only is biased toward early stop-outs (winners are still open); mark open ones at the current price
        marked = [(o["side"] * o["qty"] * (prices[o["symbol"]] - o["entry_price"]) - o["fees"]
                   - o["qty"] * prices[o["symbol"]] * COST_SIDE) / NOTIONAL for o in opn if o["symbol"] in prices]
        if marked:
            print(f"        incl. open at mark (no funding): {_stats(rets + marked)}")
        if rows:
            srt = sorted(rets)
            # R = pnl / risk at the stop; ATR% = stop distance / 2.5 / entry
            rs = [r["pnl"] / (r["qty"] * abs(r["entry_price"] - r["stop"])) for r in rows]
            calm = [r["pnl"] / NOTIONAL for r in rows
                    if abs(r["entry_price"] - r["stop"]) / 2.5 / r["entry_price"] <= 0.08]
            print(f"        median {srt[len(srt) // 2] * 1e4:+.1f}bp | ex ATR>8% mean "
                  f"{(sum(calm) / len(calm) * 1e4 if calm else 0):+.1f}bp (n={len(calm)}) | mean R {sum(rs) / len(rs):+.3f}")
    for ver, in con.execute("SELECT DISTINCT ai_version FROM signals WHERE ai_version IS NOT NULL"):
        rets = [r["pnl"] / NOTIONAL for r in con.execute(
            "SELECT p.pnl FROM positions p JOIN signals s ON s.id=p.signal_id WHERE p.book='AI' AND p.status='closed' "
            "AND s.ai_version=?", (ver,))]
        print(f"  AI {ver}: {_stats(rets)}")
    rows = con.execute("SELECT exit_reason, book, COUNT(*) n, AVG(pnl) a FROM positions WHERE status='closed' "
                       "GROUP BY book, exit_reason").fetchall()
    for r in rows:
        print(f"  {r['book']:<5} {r['exit_reason']:<18} n={r['n']:<4} avg {r['a'] / NOTIONAL * 1e4:+.1f}bp")
    dec = con.execute("SELECT ai_status, COUNT(*) FROM signals GROUP BY ai_status").fetchall()
    print("  AI decisions:", {r[0]: r[1] for r in dec})


def manual_report(_args):
    """The user's manual testnet trades (testnet_sync.py), grouped by what the pick rule and execution can change."""
    con = db()
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='manual_trades'").fetchone():
        raise SystemExit("no manual_trades yet (run testnet_sync.py)")
    rows = [dict(r) for r in con.execute("SELECT * FROM manual_trades ORDER BY entry_time")]
    for r in rows:
        r["f"] = json.loads(r["features"]) if r["features"] else {}
    for r in rows:
        if r["status"] == "open":
            print(f"OPEN {r['symbol']} {'L' if r['side'] > 0 else 'S'} since {iso(r['entry_time'])} entry {r['entry_price']:.6g} "
                  f"qty {r['qty']:.6g} mark {r['mark'] or 0:.6g} uPnL {r['net']:+.2f} SL {r['sl_last']} grade {r['pick_grade']}")
    closed = [r for r in rows if r["status"] == "closed"]

    def line(name, sub):
        nets = [r["net"] for r in sub]
        rets = [r["ret_pct"] for r in sub if r["ret_pct"] is not None]
        rs = [r["r_mult"] for r in sub if r["r_mult"] is not None]
        win = sum(n > 0 for n in nets) / len(nets)
        print(f"  {name:<22} n={len(sub):<4} win {win:.0%} net {sum(nets):+9.2f} USDT avg ret "
              f"{(sum(rets) / len(rets) * 1e4 if rets else 0):+7.1f}bp"
              + (f" avg R {sum(rs) / len(rs):+.2f} (n={len(rs)})" if rs else ""))

    print(f"closed {len(closed)}")
    if not closed:
        return
    line("all", closed)
    groups = {
        "grade": lambda r: r["pick_grade"] or "no signal",
        "exit": lambda r: r["exit_type"],
        "side": lambda r: "long" if r["side"] > 0 else "short",
        "ATR": lambda r: "?" if "atr_pct" not in r["f"] else ("<=1.3%" if r["f"]["atr_pct"] <= 0.013 else ">1.3%"),
        "delay": lambda r: "?" if r["delay_min"] is None else ("<=30m" if r["delay_min"] <= 30 else
                                                              "30m-3h" if r["delay_min"] <= 180 else ">3h"),
        "SL set": lambda r: "yes" if r["sl_first"] else "no",
        "SL moved": lambda r: "yes" if r["sl_moves"] else "no",
    }
    for gname, fn in groups.items():
        print(gname)
        g = {}
        for r in closed:
            g.setdefault(fn(r), []).append(r)
        for k in sorted(g):
            line(str(k), g[k])
    pairs = []
    for r in closed:
        if r["signal_id"]:
            p = con.execute("SELECT pnl FROM positions WHERE book='ALLX' AND signal_id=? AND status='closed'",
                            (r["signal_id"],)).fetchone()
            if p:
                pairs.append((r["net"] / r["notional"], p["pnl"] / NOTIONAL))
    if pairs:
        print(f"vs mechanical ALLX exit on the same signals: n={len(pairs)} you {sum(a for a, _ in pairs) / len(pairs) * 1e4:+.1f}bp"
              f" ALLX {sum(b for _, b in pairs) / len(pairs) * 1e4:+.1f}bp")
    eff = [r["ret_pct"] / r["mfe_pct"] for r in closed if r["mfe_pct"] and r["ret_pct"] is not None]
    if eff:
        print(f"captured share of max favourable move: {sum(eff) / len(eff):.0%}")


def backfill_picks(_args):
    """Apply the pick rule to batches registered before it existed (features only, no look-ahead)."""
    con = db()
    batches: dict[int, list[int]] = {}
    for r in con.execute("SELECT id, registered_at FROM signals WHERE pick_version IS NULL"):
        batches.setdefault(r["registered_at"] // HOUR, []).append(r["id"])
    for ids in batches.values():
        assign_picks(con, ids)
    con.commit()
    print(f"backfilled {len(batches)} batches")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill_picks").set_defaults(fn=backfill_picks)
    sub.add_parser("manual").set_defaults(fn=manual_report)
    sub.add_parser("hourly").set_defaults(fn=hourly)
    sub.add_parser("pending").set_defaults(fn=pending)
    d = sub.add_parser("decide")
    d.add_argument("id", type=int)
    d.add_argument("action", choices=["take", "skip"])
    d.add_argument("--conf", type=float, default=None)
    d.add_argument("--note", default=None)
    d.set_defaults(fn=decide)
    sub.add_parser("review").set_defaults(fn=review)
    c = sub.add_parser("close")
    c.add_argument("id", type=int)
    c.add_argument("--note", default=None)
    c.set_defaults(fn=close_cmd)
    sub.add_parser("report").set_defaults(fn=report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
