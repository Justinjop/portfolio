"""Sync the user's MANUAL trades on the Binance futures demo/testnet account into desk.sqlite (read-only API use).

Runs after every `desk.py hourly` scan, or by hand: python testnet_sync.py [--days 7]
- Pulls fills (userTrades), orders (allOrders), income (realized PnL / commission / funding) and positions.
- Ignores bfbot's own testnet orders (clientOrderId prefix "bfT-").
- Rebuilds round trips (flat -> position -> flat) per symbol into table manual_trades, linked to the latest desk
  signal for the same symbol/side within 24h before entry, with pick grade, features, delay, slippage, SL/TP orders,
  exit type (stop order / TP order / manual / liquidation), MFE/MAE and R.
Keys: BINANCE_TESTNET_API_KEY/SECRET from bfbot's .env (never printed). Only GET endpoints are called.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "https://demo-fapi.binance.com"
ENV = Path(os.environ.get("DESK_TESTNET_ENV", str(Path.home() / ".config/desk/testnet.env")))
BOT_PREFIX = "bfT-"
AUTO_PREFIX = "dsk-"                    # auto.py (VM auto-executor) orders
AUTO_DB = HERE.parent / "auto" / "auto.sqlite"
BOT_SYMBOLS = {"BTCUSDT", "ETHUSDT"}    # bfbot TESTNET trades these; manual trades there would collide with it
HOUR, DAY = 3_600_000, 86_400_000
STOP_TYPES = {"STOP_MARKET", "STOP"}
TP_TYPES = {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tn_fills(id TEXT PRIMARY KEY, symbol TEXT, order_id INTEGER, side TEXT, price REAL,
    qty REAL, commission REAL, commission_asset TEXT, realized_pnl REAL, maker INTEGER, time INTEGER);
CREATE TABLE IF NOT EXISTS tn_orders(order_id INTEGER PRIMARY KEY, symbol TEXT, client_id TEXT, type TEXT,
    orig_type TEXT, side TEXT, price REAL, avg_price REAL, stop_price REAL, status TEXT, reduce_only INTEGER,
    close_position INTEGER, time INTEGER, update_time INTEGER);
CREATE TABLE IF NOT EXISTS tn_income(id TEXT PRIMARY KEY, symbol TEXT, type TEXT, income REAL, asset TEXT, time INTEGER);
CREATE TABLE IF NOT EXISTS tn_state(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS manual_trades(id INTEGER PRIMARY KEY, symbol TEXT, side INTEGER, status TEXT,
    entry_time INTEGER, entry_price REAL, qty REAL, notional REAL, exit_time INTEGER, exit_price REAL,
    realized REAL, commission REAL, funding REAL, net REAL, ret_pct REAL, n_fills INTEGER, entry_type TEXT,
    exit_type TEXT, sl_first REAL, sl_last REAL, sl_moves INTEGER, tp_first REAL, tp_last REAL,
    signal_id INTEGER, pick_grade TEXT, user_pick INTEGER, pick_rank INTEGER, batch_size INTEGER,
    signal_price REAL, delay_min REAL, slip_pct REAL, features TEXT, mfe_pct REAL, mae_pct REAL, r_mult REAL,
    hold_h REAL, unrealized REAL, mark REAL, first_fill_id TEXT UNIQUE, updated_at INTEGER);
"""


def now_ms():
    return int(time.time() * 1000)


def creds():
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    key, sec = env.get("BINANCE_TESTNET_API_KEY"), env.get("BINANCE_TESTNET_API_SECRET")
    if not key or not sec:
        raise SystemExit("testnet keys not found in bfbot .env")
    return key, sec


class Api:
    def __init__(self):
        self.key, self.sec = creds()
        self.offset = int(self.get("/fapi/v1/time", signed=False)["serverTime"]) - now_ms()

    def get(self, path, signed=True, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if signed:
            params.update(timestamp=now_ms() + self.offset, recvWindow=10000)
            q = urllib.parse.urlencode(params)
            q += "&signature=" + hmac.new(self.sec.encode(), q.encode(), hashlib.sha256).hexdigest()
        else:
            q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{BASE}{path}?{q}", headers={"X-MBX-APIKEY": self.key} if signed else {})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:200]
                if e.code in (418, 429) or attempt == 2:
                    raise RuntimeError(f"{path} HTTP {e.code}: {body}") from None
                time.sleep(2)


def state(con, k, default=None):
    r = con.execute("SELECT v FROM tn_state WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_state(con, k, v):
    con.execute("INSERT INTO tn_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ── pull ──
def pull(api, con, days):
    t_end = now_ms() + api.offset
    start = int(state(con, "income_from", t_end - days * DAY))
    syms = set()
    t = start
    while t < t_end:   # income is all-symbol; it reveals every symbol with a fill (COMMISSION) or funding
        rows = api.get("/fapi/v1/income", startTime=t, endTime=min(t + 7 * DAY, t_end), limit=1000)
        for r in rows:
            con.execute("INSERT OR IGNORE INTO tn_income VALUES(?,?,?,?,?,?)",
                        (f"{r['tranId']}-{r['incomeType']}-{r.get('tradeId', '')}", r["symbol"], r["incomeType"],
                         float(r["income"]), r["asset"], int(r["time"])))
            if r["symbol"]:
                syms.add(r["symbol"])
        if len(rows) == 1000:
            t = int(rows[-1]["time"])
        else:
            t = min(t + 7 * DAY, t_end)
    positions = [p for p in api.get("/fapi/v2/positionRisk") if float(p["positionAmt"]) != 0]
    syms |= {p["symbol"] for p in positions}
    syms |= {o["symbol"] for o in api.get("/fapi/v1/openOrders")}
    for sym in sorted(syms):
        s0 = int(state(con, f"fills_from:{sym}", start))
        t = s0
        last = s0
        while t < t_end:
            rows = api.get("/fapi/v1/userTrades", symbol=sym, startTime=t, endTime=min(t + 7 * DAY - 1, t_end),
                           limit=1000)
            for f in rows:
                con.execute("INSERT OR IGNORE INTO tn_fills VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (f"{sym}-{f['id']}", sym, int(f["orderId"]), f["side"], float(f["price"]), float(f["qty"]),
                             float(f["commission"]), f["commissionAsset"], float(f["realizedPnl"]), int(f["maker"]),
                             int(f["time"])))
                last = max(last, int(f["time"]))
            t = int(rows[-1]["time"]) + 1 if len(rows) == 1000 else min(t + 7 * DAY, t_end)
        all_orders, t = [], s0 - HOUR
        while t < t_end:
            all_orders += api.get("/fapi/v1/allOrders", symbol=sym, startTime=t, endTime=min(t + 7 * DAY - 1, t_end),
                                  limit=1000)
            t += 7 * DAY
        for o in all_orders:
            con.execute("INSERT OR REPLACE INTO tn_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (int(o["orderId"]), sym, o["clientOrderId"], o["type"], o.get("origType", o["type"]),
                         o["side"], float(o["price"]), float(o.get("avgPrice") or 0), float(o.get("stopPrice") or 0),
                         o["status"], int(bool(o.get("reduceOnly"))), int(bool(o.get("closePosition"))),
                         int(o["time"]), int(o["updateTime"])))
        # re-read the last 2h next time: fills of a still-open order can arrive late
        set_state(con, f"fills_from:{sym}", max(s0, last - 2 * HOUR))
    set_state(con, "income_from", t_end - 2 * HOUR)
    return {p["symbol"]: p for p in positions}


# ── rebuild round trips ──
def klines(sym, t0, t1):
    out, t = [], t0 - t0 % 300_000
    while t < t1:
        q = urllib.parse.urlencode({"symbol": sym, "interval": "5m", "startTime": t, "endTime": t1, "limit": 1500})
        with urllib.request.urlopen(f"{BASE}/fapi/v1/klines?{q}", timeout=20) as r:
            part = json.loads(r.read().decode())
        if not part:
            break
        out += part
        t = int(part[-1][0]) + 1
        if len(part) < 1500:
            break
    return out


def rebuild(con, positions):
    orders = {r["order_id"]: dict(r) for r in con.execute("SELECT * FROM tn_orders")}
    fills = con.execute("SELECT * FROM tn_fills ORDER BY symbol, time, id").fetchall()

    auto = []
    if AUTO_DB.exists():   # windows of the auto-executor's trades: their triggered stops carry other client ids
        import sqlite3
        ac = sqlite3.connect(f"file:{AUTO_DB}?mode=ro", uri=True)
        auto = ac.execute("SELECT symbol, entry_time - 60000, COALESCE(exit_time, 9e15) + 60000 FROM trades "
                          "WHERE entry_time IS NOT NULL").fetchall()
        ac.close()

    def is_manual(f):
        if any(f["symbol"] == s and a <= f["time"] <= b for s, a, b in auto):
            return False
        o = orders.get(f["order_id"])
        if o is None:     # order not retrieved: only trust it as manual off bfbot's symbols
            return f["symbol"] not in BOT_SYMBOLS
        return not o["client_id"].startswith((BOT_PREFIX, AUTO_PREFIX))
    manual = [f for f in fills if is_manual(f)]
    trips, cur, pos = [], {}, {}
    for f in manual:
        sym = f["symbol"]
        q = f["qty"] * (1 if f["side"] == "BUY" else -1)
        p0 = pos.get(sym, 0.0)
        remaining = q
        while abs(remaining) > 1e-12:
            if abs(p0) < 1e-12:   # flat -> open
                cur[sym] = {"symbol": sym, "side": 1 if remaining > 0 else -1, "fills": [], "entry": [], "exit": []}
            tr = cur[sym]
            if (remaining > 0) == (tr["side"] > 0):          # adds to the position
                take = remaining
                tr["entry"].append((f, abs(take)))
            else:                                            # reduces; may flip
                take = -tr["side"] * min(abs(remaining), abs(p0))
                tr["exit"].append((f, abs(take)))
            tr["fills"].append(f)
            p0 += take
            remaining -= take
            if abs(p0) < 1e-9 * max(1.0, abs(q)):
                p0 = 0.0
                trips.append(tr)
                del cur[sym]
        pos[sym] = p0
    trips += list(cur.values())
    con.execute("DELETE FROM manual_trades")
    for tr in trips:
        save_trip(con, tr, orders, positions)


def save_trip(con, tr, orders, positions):
    sym, side = tr["symbol"], tr["side"]
    eq = sum(q for _, q in tr["entry"])
    entry_px = sum(f["price"] * q for f, q in tr["entry"]) / eq
    xq = sum(q for _, q in tr["exit"])
    closed = abs(xq - eq) <= 1e-9 * max(1.0, eq)
    exit_px = sum(f["price"] * q for f, q in tr["exit"]) / xq if xq else None
    t0 = tr["entry"][0][0]["time"]
    t1 = tr["exit"][-1][0]["time"] if closed else None
    # a flipping fill is split by qty share for commission; its realizedPnl belongs wholly to the closing part
    share = lambda f, q: q / f["qty"] if f["qty"] else 1   # noqa: E731
    realized = sum(f["realized_pnl"] for f, _ in tr["exit"])
    commission = sum((f["commission"] if f["commission_asset"] == "USDT" else 0) * share(f, q)
                     for f, q in tr["entry"] + tr["exit"])
    fund = sum(r[0] for r in con.execute("SELECT income FROM tn_income WHERE symbol=? AND type='FUNDING_FEE' AND "
                                         "time>? AND time<=?", (sym, t0, t1 or now_ms())))
    mark = unreal = None
    if not closed and sym in positions:
        p = positions[sym]
        mark, unreal = float(p["markPrice"]), float(p["unRealizedProfit"])
    net = realized + fund - commission + (unreal or 0)
    # stop / take-profit orders placed while the trade was open
    end = t1 or now_ms()
    prot = sorted((o for o in orders.values() if o["symbol"] == sym and t0 - 60_000 <= o["time"] <= end
                   and not o["client_id"].startswith((BOT_PREFIX, AUTO_PREFIX))), key=lambda o: o["time"])
    sls = [o["stop_price"] for o in prot if o["orig_type"] in STOP_TYPES and o["stop_price"]]
    tps = [o["stop_price"] or o["price"] for o in prot if o["orig_type"] in TP_TYPES]
    last_order = orders.get(tr["exit"][-1][0]["order_id"]) if tr["exit"] else None
    first_order = orders.get(tr["entry"][0][0]["order_id"])
    if not closed:
        exit_type = None
    elif last_order is None:
        exit_type = "未知"
    elif last_order["client_id"].startswith(("autoclose-", "adl_autoclose")):
        exit_type = "強平／ADL"
    elif last_order["orig_type"] in STOP_TYPES:
        exit_type = "停損單"
    elif last_order["orig_type"] in TP_TYPES:
        exit_type = "停利單"
    elif last_order["orig_type"] == "TRAILING_STOP_MARKET":
        exit_type = "追蹤停損"
    else:
        exit_type = "手動" + ("市價" if last_order["orig_type"] == "MARKET" else "限價")
    sig = con.execute("SELECT * FROM signals WHERE symbol=? AND side=? AND registered_at<=? AND registered_at>=? "
                      "ORDER BY registered_at DESC LIMIT 1", (sym, side, t0 + 60_000, t0 - 24 * HOUR)).fetchone()
    mfe = mae = None
    try:
        kl = klines(sym, t0, end)
        if kl:
            mfe = max(side * ((float(k[2]) if side > 0 else float(k[3])) - entry_px) / entry_px for k in kl)
            mae = min(side * ((float(k[3]) if side > 0 else float(k[2])) - entry_px) / entry_px for k in kl)
    except Exception:
        pass
    risk = abs(entry_px - sls[0]) * eq if sls else None
    ref = exit_px if closed else mark
    con.execute(
        "INSERT INTO manual_trades(symbol,side,status,entry_time,entry_price,qty,notional,exit_time,exit_price,realized,"
        "commission,funding,net,ret_pct,n_fills,entry_type,exit_type,sl_first,sl_last,sl_moves,tp_first,tp_last,signal_id,"
        "pick_grade,user_pick,pick_rank,batch_size,signal_price,delay_min,slip_pct,features,mfe_pct,mae_pct,r_mult,hold_h,"
        "unrealized,mark,first_fill_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?)",
        (sym, side, "closed" if closed else "open", t0, entry_px, eq, entry_px * eq, t1, exit_px, realized, commission,
         fund, net, side * (ref - entry_px) / entry_px if ref else None, len(tr["fills"]),
         first_order["orig_type"] if first_order else None, exit_type,
         sls[0] if sls else None, sls[-1] if sls else None, max(0, len(set(sls)) - 1),
         tps[0] if tps else None, tps[-1] if tps else None,
         sig["id"] if sig else None, sig["pick_grade"] if sig else None, sig["user_pick"] if sig else None,
         sig["pick_rank"] if sig else None, sig["batch_size"] if sig else None, sig["price"] if sig else None,
         (t0 - sig["registered_at"]) / 60000 if sig else None,
         side * (entry_px - sig["price"]) / sig["price"] if sig else None, sig["features"] if sig else None,
         mfe, mae, net / risk if risk else None, ((t1 or now_ms()) - t0) / HOUR, unreal, mark,
         tr["fills"][0]["id"], now_ms()))


def sync(con, days=7):
    con.executescript(SCHEMA)
    api = Api()
    positions = pull(api, con, days)
    rebuild(con, positions)
    con.commit()
    n = con.execute("SELECT status, COUNT(*) FROM manual_trades GROUP BY status").fetchall()
    return {r[0]: r[1] for r in n}


if __name__ == "__main__":
    import sqlite3
    c = sqlite3.connect(HERE / "desk.sqlite")
    c.row_factory = sqlite3.Row
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
    print(sync(c, days))
