"""Auto-executor: trades every Signal Desk pick (desk signals.user_pick=1) on the Binance futures DEMO account.

TESTNET ONLY. The base URL is fixed to demo-fapi and the keys come from ~/.config/desk/testnet.env. There is no
switch to production in this file, and none may be added without the user's explicit authorization.

python auto.py run                 every 5 min (desk-auto.timer): reconcile, protect stops, hourly exits, new entries
python auto.py status              account, open trades, policy, halt/pause state
python auto.py report [--days N]   closed-trade stats in R (by grade / exit / side), execution and skip reasons
python auto.py whatif [--sl 2.5] [--tp none] [--be none] [--hold 72] [--opp 1] [--days N]
                                   replays a policy on every P1 pick (entry at the signal price, 5m bars)
python auto.py policy              current policy, bounds and change log
python auto.py set KEY=VALUE [...] --reason "..."   bounds, step size and cadence are enforced here
python auto.py pause | resume | flatten            (user only) stop entries / resume / close everything

Hard rules in code (the tuning agent cannot change them): demo-fapi only, every position gets an exchange-side
stop (or is closed), bfbot's BTC/ETH and symbols with someone else's position are never traded, the policy is
clamped to BOUNDS, a daily-loss breaker halts entries for 24h.
"""
import fcntl
import hashlib
import hmac
import json
import math
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

HERE = Path(__file__).resolve().parent
QT = HERE.parent
DESK_DB = QT / "desk" / "desk.sqlite"
DB = HERE / "auto.sqlite"
POLICY = HERE / "policy.json"
PAUSE = HERE / "PAUSE"
LOCK = HERE / "auto.lock"
ENV = Path(os.environ.get("DESK_TESTNET_ENV", str(Path.home() / ".config/desk/testnet.env")))
BASE = "https://demo-fapi.binance.com"          # TESTNET ONLY
PROD_PUBLIC = "https://fapi.binance.com"        # unsigned public klines for whatif only
PREFIX = "dsk-"
BOT_SYMBOLS = {"BTCUSDT", "ETHUSDT"}            # bfbot TESTNET trades these on the same account
LEVERAGE = 5                                    # cross margin; size comes from risk, leverage only sets margin use
HOUR, DAY, FIVE = 3_600_000, 86_400_000, 300_000
MAX_AGE_MS = 2 * HOUR                           # picks older than this are not entered
MAX_DIVERGENCE = 0.015                          # demo mark vs desk signal price
BE_BUFFER = 0.001                               # breakeven stop sits 0.1% past entry (covers fees)
COST_SIDE = 0.0010                              # whatif cost per side, same as the paper books
WHATIF_PER_DAY = 8
PATH_BARS = 1500                                # 125h of 5m bars per pick (max_hold_h bound is 120)

DEFAULT_POLICY = {"version": 1, "risk_pct": 0.25, "risk_mult_A": 1.0, "risk_mult_B": 1.0, "sl_atr": 2.5,
                  "tp_atr": None, "be_atr": None, "max_hold_h": 72, "opposite_exit": True, "max_open": 30,
                  "max_same_side": 20, "max_gross_x": 2.5, "max_notional_pct": 25, "daily_stop_pct": 5}
BOUNDS = {"risk_pct": (0.05, 1.0), "risk_mult_A": (0.25, 1.5), "risk_mult_B": (0.25, 1.5), "sl_atr": (1.0, 4.0),
          "tp_atr": (1.5, 12.0), "be_atr": (0.5, 6.0), "max_hold_h": (6, 120), "opposite_exit": None,
          "max_open": (1, 40), "max_same_side": (1, 30), "max_gross_x": (0.5, 3.0), "max_notional_pct": (5, 40),
          "daily_stop_pct": (2, 8)}
NULLABLE = {"tp_atr", "be_atr"}
INTS = {"max_hold_h", "max_open", "max_same_side"}
DERISK = {"risk_pct", "risk_mult_A", "risk_mult_B", "max_open", "max_same_side", "max_gross_x", "max_notional_pct",
          "daily_stop_pct"}                     # lowering any of these only reduces risk
CHANGE_MIN_TRADES, CHANGE_MIN_DAYS = 30, 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, signal_id INTEGER UNIQUE, symbol TEXT, side INTEGER,
    grade TEXT, policy_version INTEGER, status TEXT, note TEXT, created_at INTEGER, sig_price REAL, atr_pct REAL,
    entry_order INTEGER, entry_time INTEGER, entry_price REAL, qty REAL, notional REAL, risk_usdt REAL,
    stop_init REAL, stop_cur REAL, stop_n INTEGER DEFAULT 0, tp REAL, tp_n INTEGER DEFAULT 0, be_done INTEGER DEFAULT 0,
    best_px REAL, exit_n INTEGER DEFAULT 0, exit_time INTEGER, exit_price REAL, exit_reason TEXT, realized REAL,
    commission REAL, funding REAL, net REAL, r_mult REAL, mfe_atr REAL, mae_atr REAL, slip_pct REAL);
CREATE TABLE IF NOT EXISTS equity(ts INTEGER PRIMARY KEY, equity REAL, wallet REAL, upnl REAL, open_n INTEGER, gross REAL);
CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS policy_log(ts INTEGER, version INTEGER, kind TEXT, changes TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS paths(signal_id INTEGER PRIMARY KEY, complete INTEGER, data BLOB);
"""
LIVE = ("opening", "open", "closing")


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else "-"


class ApiError(Exception):
    def __init__(self, status, body, path, unknown=False):
        super().__init__(f"{path} HTTP {status}: {body}")
        self.status, self.unknown = status, unknown
        try:
            self.code = json.loads(body).get("code")
        except Exception:
            self.code = None


class Api:
    def __init__(self):
        assert BASE == "https://demo-fapi.binance.com", "auto.py trades the demo account only"
        env = {}
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        self.key, self.sec = env.get("BINANCE_TESTNET_API_KEY"), env.get("BINANCE_TESTNET_API_SECRET")
        if not self.key or not self.sec:
            raise SystemExit("testnet keys not found")
        self.offset = int(self.req("GET", "/fapi/v1/time", signed=False)["serverTime"]) - now_ms()

    def req(self, method, path, signed=True, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if signed:
            params.update(timestamp=now_ms() + getattr(self, "offset", 0), recvWindow=10000)
        q = urllib.parse.urlencode(params)
        if signed:
            q += "&signature=" + hmac.new(self.sec.encode(), q.encode(), hashlib.sha256).hexdigest()
        tries = 3 if method == "GET" else 1      # never blind-retry an order: reconcile resolves unknowns
        for attempt in range(tries):
            rq = urllib.request.Request(f"{BASE}{path}?{q}", method=method,
                                        headers={"X-MBX-APIKEY": self.key} if signed else {})
            try:
                with urllib.request.urlopen(rq, timeout=20) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                if method != "GET" or e.code in (400, 401, 403, 418, 429) or attempt == tries - 1:
                    raise ApiError(e.code, body, path) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if method != "GET" or attempt == tries - 1:
                    raise ApiError(0, repr(e), path, unknown=True) from None
            time.sleep(2)


def public(base, path, **params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SignalDesk"}), timeout=20) as r:
        return json.loads(r.read())


# ── storage / policy ──
def adb():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if "fetched" not in {r[1] for r in con.execute("PRAGMA table_info(paths)")}:
        con.execute("ALTER TABLE paths ADD COLUMN fetched INTEGER DEFAULT 0")
    return con


def desk():
    con = sqlite3.connect(f"file:{DESK_DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def get_state(con, k, default=None):
    r = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_state(con, k, v):
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def event(con, kind, info):
    con.execute("INSERT INTO events VALUES(?,?,?)", (now_ms(), kind, str(info)[:800]))


def clamp_policy(p):
    out = dict(DEFAULT_POLICY)
    out.update({k: v for k, v in p.items() if k in DEFAULT_POLICY})
    for k, b in BOUNDS.items():
        v = out[k]
        if b is None:
            out[k] = bool(v)
        elif v is None and k in NULLABLE:
            continue
        else:
            v = min(max(float(v), b[0]), b[1])
            out[k] = int(round(v)) if k in INTS else v
    return out


def load_policy():
    if not POLICY.exists():
        save_policy(DEFAULT_POLICY)
    return clamp_policy(json.loads(POLICY.read_text()))


def save_policy(p):
    tmp = POLICY.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False))
    tmp.replace(POLICY)


# ── exchange helpers ──
def snapshot(api):
    acct = api.req("GET", "/fapi/v3/account")
    pos = {p["symbol"]: p for p in api.req("GET", "/fapi/v3/positionRisk") if float(p["positionAmt"]) != 0}
    orders = api.req("GET", "/fapi/v1/openOrders")
    algos = api.req("GET", "/fapi/v1/openAlgoOrders")
    algos = algos if isinstance(algos, list) else algos.get("orders", [])
    wallet, upnl = float(acct["totalWalletBalance"]), float(acct["totalUnrealizedProfit"])
    return {"wallet": wallet, "upnl": upnl, "equity": wallet + upnl, "avail": float(acct["availableBalance"]),
            "pos": pos, "orders": orders, "algos": algos,
            "gross": sum(abs(float(p["positionAmt"])) * float(p["markPrice"]) for p in pos.values())}


def filters(api, con):
    cached = get_state(con, "exinfo")
    if cached:
        ts, data = json.loads(cached)
        if now_ms() - ts < 6 * HOUR:
            return data
    data = {}
    for s in api.req("GET", "/fapi/v1/exchangeInfo", signed=False)["symbols"]:
        f = {x["filterType"]: x for x in s["filters"]}
        data[s["symbol"]] = {"trading": s["status"] == "TRADING" and s.get("contractType") == "PERPETUAL",
                             "tick": f["PRICE_FILTER"]["tickSize"], "step": f["MARKET_LOT_SIZE"]["stepSize"],
                             "min_qty": f["MARKET_LOT_SIZE"]["minQty"],
                             "max_qty": min(float(f["MARKET_LOT_SIZE"]["maxQty"]), float(f["LOT_SIZE"]["maxQty"])),
                             "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5))}
    set_state(con, "exinfo", json.dumps([now_ms(), data]))
    return data


def round_step(x, step):
    s = Decimal(step)
    return (Decimal(str(x)) / s).to_integral_value(ROUND_DOWN) * s


def round_tick(x, tick):
    t = Decimal(tick)
    return (Decimal(str(x)) / t).to_integral_value() * t


def fmt(d):
    return format(d.normalize(), "f")


def place_stop(api, con, tr, fl, price, take_profit=False):
    n_col = "tp_n" if take_profit else "stop_n"
    n = (tr[n_col] or 0) + 1
    cid = f"{PREFIX}{tr['id']}-{'t' if take_profit else 's'}{n}"
    con.execute(f"UPDATE trades SET {n_col}=? WHERE id=?", (n, tr["id"]))
    con.commit()
    api.req("POST", "/fapi/v1/algoOrder", algoType="CONDITIONAL", symbol=tr["symbol"],
            side="SELL" if tr["side"] > 0 else "BUY", type="TAKE_PROFIT_MARKET" if take_profit else "STOP_MARKET",
            quantity=fmt(round_step(tr["qty"], fl["step"])), triggerPrice=fmt(round_tick(price, fl["tick"])),
            workingType="MARK_PRICE", priceProtect="true", reduceOnly="true", clientAlgoId=cid,
            newOrderRespType="RESULT")
    col = "tp" if take_profit else "stop_cur"
    con.execute(f"UPDATE trades SET {col}=? WHERE id=?", (float(round_tick(price, fl["tick"])), tr["id"]))
    con.commit()
    return cid


def cancel_algos(api, snap_algos, symbol):
    for a in snap_algos:
        if a["symbol"] == symbol and str(a.get("clientAlgoId", "")).startswith(PREFIX):
            try:
                api.req("DELETE", "/fapi/v1/algoOrder", clientAlgoId=a["clientAlgoId"])
            except ApiError:
                pass


def market_close(api, con, tr, qty, reason):
    n = (tr["exit_n"] or 0) + 1
    con.execute("UPDATE trades SET status='closing', exit_reason=?, exit_n=? WHERE id=?", (reason, n, tr["id"]))
    con.commit()
    api.req("POST", "/fapi/v1/order", symbol=tr["symbol"], side="SELL" if tr["side"] > 0 else "BUY", type="MARKET",
            quantity=fmt(Decimal(str(qty))), reduceOnly="true", newClientOrderId=f"{PREFIX}{tr['id']}-x{n}",
            newOrderRespType="RESULT")


def algo_status(api, cid):
    try:
        return api.req("GET", "/fapi/v1/algoOrder", clientAlgoId=cid).get("algoStatus")
    except ApiError:
        return None


def demo_klines(symbol, start, end=None, limit=1500):
    return public(BASE, "/fapi/v1/klines", symbol=symbol, interval="5m", startTime=start, endTime=end, limit=limit)


# ── run: reconcile / protect / exits / entries ──
class Run:
    def __init__(self, api, con, pol):
        self.api, self.con, self.pol = api, con, pol
        self.snap = snapshot(api)
        self.fl = None
        self.notes = []          # lines for the Discord summary

    def refresh(self):
        self.snap = snapshot(self.api)

    def filt(self):
        if self.fl is None:
            self.fl = filters(self.api, self.con)
        return self.fl

    def position(self, tr):
        p = self.snap["pos"].get(tr["symbol"])
        if p and (float(p["positionAmt"]) > 0) == (tr["side"] > 0):
            return p
        return None

    def active_algo(self, cid):
        return any(a.get("clientAlgoId") == cid for a in self.snap["algos"])

    def reconcile(self):
        con = self.con
        for tr in con.execute("SELECT * FROM trades WHERE status IN ('opening','open','closing')").fetchall():
            try:
                if tr["status"] == "opening":
                    self.resolve_opening(tr)
                    continue
                if self.position(tr) is None:
                    self.finalize(tr)
                    continue
                if tr["status"] == "open":
                    self.protect(tr)
            except ApiError as e:
                event(con, "error", f"reconcile {tr['id']} {tr['symbol']}: {e}")
                print(f"AUTO-ERROR reconcile {tr['id']} {tr['symbol']}: {e}")

    def resolve_opening(self, tr):
        try:
            o = self.api.req("GET", "/fapi/v1/order", symbol=tr["symbol"], origClientOrderId=f"{PREFIX}{tr['id']}-e")
        except ApiError as e:
            if e.code == -2013 and now_ms() - tr["created_at"] > 2 * 60_000:
                self.con.execute("UPDATE trades SET status='failed', note='entry order not found' WHERE id=?", (tr["id"],))
            return
        if o["status"] == "FILLED" or float(o.get("executedQty", 0)) > 0:
            self.after_fill(tr, o)
        elif o["status"] in ("CANCELED", "EXPIRED", "REJECTED"):
            self.con.execute("UPDATE trades SET status='failed', note=? WHERE id=?", (f"entry {o['status']}", tr["id"]))

    def after_fill(self, tr, o):
        fl = self.filt()[tr["symbol"]]
        px, qty = float(o["avgPrice"]), float(o["executedQty"])
        atr = tr["atr_pct"] * px
        stop = px - tr["side"] * self.pol["sl_atr"] * atr
        tp = px + tr["side"] * self.pol["tp_atr"] * atr if self.pol["tp_atr"] else None
        slip = tr["side"] * (px - tr["sig_price"]) / tr["sig_price"]
        self.con.execute("UPDATE trades SET status='open', entry_order=?, entry_time=?, entry_price=?, qty=?, notional=?,"
                         " stop_init=?, best_px=?, slip_pct=? WHERE id=?",
                         (int(o["orderId"]), int(o.get("updateTime") or now_ms()), px, qty, px * qty,
                          float(round_tick(stop, fl["tick"])), px, slip, tr["id"]))
        self.con.commit()
        tr = self.con.execute("SELECT * FROM trades WHERE id=?", (tr["id"],)).fetchone()
        try:
            place_stop(self.api, self.con, tr, fl, stop)
        except ApiError as e:
            event(self.con, "error", f"stop failed {tr['symbol']}: {e}")
            try:   # one retry, then never leave it unprotected
                place_stop(self.api, self.con, self.con.execute("SELECT * FROM trades WHERE id=?", (tr["id"],)).fetchone(),
                           fl, stop)
            except ApiError:
                market_close(self.api, self.con, tr, qty, "stop_failed")
                self.notes.append(f"⚠️ {tr['symbol']} 停損單下不了，已立即平倉")
                return
        if tp:
            try:
                place_stop(self.api, self.con, self.con.execute("SELECT * FROM trades WHERE id=?", (tr["id"],)).fetchone(),
                           fl, tp, take_profit=True)
            except ApiError as e:
                event(self.con, "error", f"tp failed {tr['symbol']}: {e}")
        side = "多" if tr["side"] > 0 else "空"
        self.notes.append(f"開 {side} {tr['symbol']} @ {px:.6g}，{px * qty:,.0f}U，停損 {stop:.6g}，"
                          f"風險 {tr['risk_usdt']:.1f}U（{tr['grade']}，滑價 {slip:+.2%}）")

    def protect(self, tr):
        fl = self.filt()[tr["symbol"]]
        pos = self.position(tr)
        mark = float(pos["markPrice"])
        # breakeven: track the best price on demo 5m bars
        best = tr["best_px"] or tr["entry_price"]
        try:
            for k in demo_klines(tr["symbol"], max(tr["entry_time"], now_ms() - 3 * FIVE), limit=4):
                best = max(best, float(k[2])) if tr["side"] > 0 else min(best, float(k[3]))
        except Exception:
            pass
        self.con.execute("UPDATE trades SET best_px=? WHERE id=?", (best, tr["id"]))
        atr = tr["atr_pct"] * tr["entry_price"]
        stop_cid = f"{PREFIX}{tr['id']}-s{tr['stop_n']}"
        be = self.pol["be_atr"]
        if be and not tr["be_done"] and tr["side"] * (best - tr["entry_price"]) >= be * atr:
            new_stop = tr["entry_price"] * (1 + tr["side"] * BE_BUFFER)
            if tr["side"] * (mark - new_stop) > 0:
                old = stop_cid
                place_stop(self.api, self.con, tr, fl, new_stop)
                try:
                    self.api.req("DELETE", "/fapi/v1/algoOrder", clientAlgoId=old)
                except ApiError:
                    pass
                self.con.execute("UPDATE trades SET be_done=1 WHERE id=?", (tr["id"],))
                event(self.con, "breakeven", f"{tr['symbol']} stop -> {new_stop:.6g}")
                return
        if not self.active_algo(stop_cid):
            stop = tr["stop_cur"] or tr["stop_init"]
            if tr["side"] * (mark - stop) <= 0:     # already through the stop: close now
                market_close(self.api, self.con, tr, abs(float(pos["positionAmt"])), "stop_missing")
                return
            try:
                place_stop(self.api, self.con, tr, fl, stop)
            except ApiError as e:                  # can't re-protect it: never leave it open without a stop
                event(self.con, "error", f"stop re-place failed {tr['symbol']}: {e}")
                market_close(self.api, self.con, tr, abs(float(pos["positionAmt"])), "stop_missing")
                self.notes.append(f"⚠️ {tr['symbol']} 停損單遺失且補不回去，已立即平倉")
                return
            event(self.con, "stop_replaced", f"{tr['symbol']} {stop:.6g}")

    def finalize(self, tr):
        api, con = self.api, self.con
        t0 = tr["entry_time"] or tr["created_at"]
        fills = api.req("GET", "/fapi/v1/userTrades", symbol=tr["symbol"], startTime=t0 - 5000, limit=1000)
        exit_side = "SELL" if tr["side"] > 0 else "BUY"
        closing = [f for f in fills if f["side"] == exit_side and int(f["time"]) >= t0]
        mine = [f for f in fills if int(f["orderId"]) == tr["entry_order"]] + closing
        if not closing:
            if now_ms() - t0 > 10 * 60_000:
                event(con, "error", f"{tr['symbol']} position gone but no closing fills")
            return
        q = sum(float(f["qty"]) for f in closing)
        exit_px = sum(float(f["price"]) * float(f["qty"]) for f in closing) / q
        t1 = max(int(f["time"]) for f in closing)
        realized = sum(float(f["realizedPnl"]) for f in closing)
        comm = sum(float(f["commission"]) for f in mine if f["commissionAsset"] == "USDT")
        inc = api.req("GET", "/fapi/v1/income", symbol=tr["symbol"], incomeType="FUNDING_FEE", startTime=t0,
                      endTime=t1 + 1000, limit=1000)
        fund = sum(float(r["income"]) for r in inc)
        net = realized - comm + fund
        reason = tr["exit_reason"]
        if not reason:
            fired = lambda kind, n: any(algo_status(api, f"{PREFIX}{tr['id']}-{kind}{i}") in ("TRIGGERED", "FINISHED")
                                        for i in range(n or 0, 0, -1))   # any of them: a re-placed stop may be newer
            if fired("t", tr["tp_n"]):
                reason = "tp"
            elif fired("s", tr["stop_n"]):
                reason = "breakeven" if tr["be_done"] else "stop"
            else:
                reason = "external"
        atr = tr["atr_pct"] * tr["entry_price"]
        mfe = mae = None
        try:
            kl = demo_klines(tr["symbol"], t0 - t0 % FIVE, t1)
            if kl:
                hi, lo = max(float(k[2]) for k in kl), min(float(k[3]) for k in kl)
                fav, adv = (hi, lo) if tr["side"] > 0 else (lo, hi)
                mfe = tr["side"] * (fav - tr["entry_price"]) / atr
                mae = tr["side"] * (adv - tr["entry_price"]) / atr
        except Exception:
            pass
        con.execute("UPDATE trades SET status='closed', exit_time=?, exit_price=?, exit_reason=?, realized=?, commission=?,"
                    " funding=?, net=?, r_mult=?, mfe_atr=?, mae_atr=? WHERE id=?",
                    (t1, exit_px, reason, realized, comm, fund, net, net / tr["risk_usdt"] if tr["risk_usdt"] else None,
                     mfe, mae, tr["id"]))
        cancel_algos(api, self.snap["algos"], tr["symbol"])
        con.commit()
        self.notes.append(f"平 {tr['symbol']}（{REASON_ZH.get(reason, reason)}）{net:+.2f}U = "
                          f"{net / tr['risk_usdt']:+.2f}R，持有 {(t1 - t0) / HOUR:.1f}h")

    def exits(self):
        hour_id = now_ms() // HOUR
        if time.gmtime().tm_min < 4 or get_state(self.con, "exit_hour") == str(hour_id):
            return
        set_state(self.con, "exit_hour", hour_id)
        dk = desk()
        for tr in self.con.execute("SELECT * FROM trades WHERE status='open'").fetchall():
            pos = self.position(tr)
            if pos is None:
                continue
            reason = None
            if now_ms() - tr["entry_time"] >= self.pol["max_hold_h"] * HOUR:
                reason = "time"
            elif self.pol["opposite_exit"] and dk.execute(
                    "SELECT 1 FROM signals WHERE symbol=? AND side=? AND registered_at>?",
                    (tr["symbol"], -tr["side"], tr["entry_time"])).fetchone():
                reason = "opposite"
            if reason:
                try:
                    market_close(self.api, self.con, tr, abs(float(pos["positionAmt"])), reason)
                except ApiError as e:
                    event(self.con, "error", f"exit {tr['symbol']}: {e}")
                    print(f"AUTO-ERROR exit {tr['symbol']}: {e}")
        dk.close()
        self.con.commit()

    def halted(self):
        con, pol = self.con, self.pol
        until = int(get_state(con, "halt_until", 0))
        if now_ms() < until:
            return True
        peak = con.execute("SELECT MAX(equity) FROM equity WHERE ts>?", (now_ms() - DAY,)).fetchone()[0]
        if peak and self.snap["equity"] < peak * (1 - pol["daily_stop_pct"] / 100):
            set_state(con, "halt_until", now_ms() + DAY)
            event(con, "halt", f"equity {self.snap['equity']:.0f} < 24h peak {peak:.0f} -{pol['daily_stop_pct']}%")
            self.notes.append(f"🛑 熔斷：權益 {self.snap['equity']:.0f}U 比 24 小時高點 {peak:.0f}U 跌超過 "
                              f"{pol['daily_stop_pct']}%，24 小時內不開新單（停損照常運作）")
            return True
        return False

    def entries(self):
        con, pol = self.con, self.pol
        start = int(get_state(con, "start_ms") or 0)
        if not start:
            start = now_ms()
            set_state(con, "start_ms", start)
            con.commit()
        dk = desk()
        sigs = dk.execute("SELECT id, symbol, side, price, features, pick_grade, registered_at FROM signals "
                          "WHERE user_pick=1 AND registered_at>=? ORDER BY registered_at, pick_rank", (start,)).fetchall()
        dk.close()
        done = {r[0] for r in con.execute("SELECT signal_id FROM trades")}
        sigs = [s for s in sigs if s["id"] not in done]
        if not sigs:
            return
        halted, paused = self.halted(), PAUSE.exists()
        marks = {r["symbol"]: float(r["markPrice"]) for r in public(BASE, "/fapi/v1/premiumIndex")}
        fl = self.filt()
        foreign = {o["symbol"] for o in self.snap["orders"] if not o["clientOrderId"].startswith(PREFIX)}
        live = {r["symbol"]: r["side"] for r in con.execute(f"SELECT symbol, side FROM trades WHERE status IN {LIVE}")}
        foreign |= {s for s in self.snap["pos"] if s not in live}
        skipped = {}
        for s in sigs:
            f = json.loads(s["features"])
            reason = self.gate(s, f, marks, fl, foreign, live, halted, paused)
            if reason:
                self.insert(s, f, "skipped", reason)
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            try:
                if self.open(s, f, marks[s["symbol"]], fl[s["symbol"]]):
                    live[s["symbol"]] = s["side"]
            except ApiError as e:
                event(con, "error", f"entry {s['symbol']}: {e}")
                print(f"AUTO-ERROR entry {s['symbol']}: {e}")
            con.commit()
        if skipped:
            self.notes.append("略過：" + "、".join(f"{SKIP_ZH.get(k, k)} {v}" for k, v in skipped.items()))

    def gate(self, s, f, marks, fl, foreign, live, halted, paused):
        sym, pol = s["symbol"], self.pol
        if paused:
            return "paused"
        if halted:
            return "halted"
        if now_ms() - s["registered_at"] > MAX_AGE_MS:
            return "stale"
        if sym in BOT_SYMBOLS:
            return "bfbot_symbol"
        if sym not in fl or not fl[sym]["trading"] or sym not in marks:
            return "not_on_demo"
        if sym in foreign:
            return "foreign_position"
        if sym in live:
            return "already_open"
        if abs(marks[sym] / s["price"] - 1) > MAX_DIVERGENCE:
            return "price_divergence"
        if len(live) >= pol["max_open"]:
            return "cap_open"
        if sum(1 for v in live.values() if v == s["side"]) >= pol["max_same_side"]:
            return "cap_side"
        return None

    def size(self, s, f, px, fl):
        pol, eq = self.pol, self.snap["equity"]
        mult = pol["risk_mult_A"] if s["pick_grade"] == "A" else pol["risk_mult_B"]
        risk = eq * pol["risk_pct"] / 100 * mult
        stop_dist = pol["sl_atr"] * f["atr_pct"] * px
        qty = min(risk / stop_dist, eq * pol["max_notional_pct"] / 100 / px, fl["max_qty"])
        q = round_step(qty, fl["step"])
        return q, float(q) * stop_dist

    def open(self, s, f, px, fl):
        q, risk = self.size(s, f, px, fl)
        notional = float(q) * px
        if q < Decimal(fl["min_qty"]) or notional < fl["min_notional"] * 1.1:
            self.insert(s, f, "skipped", "too_small")
            return False
        if self.snap["gross"] + notional > self.pol["max_gross_x"] * self.snap["equity"]:
            self.insert(s, f, "skipped", "cap_gross")
            return False
        if self.snap["avail"] < notional / LEVERAGE * 1.5:
            self.insert(s, f, "skipped", "cap_margin")
            return False
        lev_key = f"lev:{s['symbol']}"
        if get_state(self.con, lev_key) != str(LEVERAGE):
            try:
                self.api.req("POST", "/fapi/v1/leverage", symbol=s["symbol"], leverage=LEVERAGE)
            except ApiError as e:
                self.insert(s, f, "skipped", "leverage_error")
                event(self.con, "error", f"leverage {s['symbol']}: {e}")
                return False
            set_state(self.con, lev_key, LEVERAGE)
        tid = self.insert(s, f, "opening", None, risk=risk, qty=float(q))
        self.con.commit()
        try:
            o = self.api.req("POST", "/fapi/v1/order", symbol=s["symbol"], side="BUY" if s["side"] > 0 else "SELL",
                             type="MARKET", quantity=fmt(q), newClientOrderId=f"{PREFIX}{tid}-e",
                             newOrderRespType="RESULT")
        except ApiError as e:
            if not e.unknown:
                self.con.execute("UPDATE trades SET status='failed', note=? WHERE id=?", (str(e)[:300], tid))
            raise                      # unknown outcome stays 'opening'; reconcile queries it next run
        if not float(o.get("avgPrice") or 0):   # demo-fapi's RESULT response can omit avgPrice
            o = self.api.req("GET", "/fapi/v1/order", symbol=s["symbol"], origClientOrderId=f"{PREFIX}{tid}-e")
        self.snap["gross"] += notional
        self.snap["avail"] -= notional / LEVERAGE
        self.after_fill(self.con.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone(), o)
        return True

    def insert(self, s, f, status, note, risk=None, qty=None):
        cur = self.con.execute(
            "INSERT INTO trades(signal_id, symbol, side, grade, policy_version, status, note, created_at, sig_price,"
            " atr_pct, risk_usdt, qty) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s["symbol"], s["side"], s["pick_grade"], self.pol["version"], status, note, now_ms(), s["price"],
             f["atr_pct"], risk, qty))
        return cur.lastrowid

    def record_equity(self):
        n = self.con.execute("SELECT COUNT(*) FROM trades WHERE status IN ('open','closing')").fetchone()[0]
        self.con.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?,?,?,?)",
                         (now_ms(), self.snap["equity"], self.snap["wallet"], self.snap["upnl"], n, self.snap["gross"]))


REASON_ZH = {"stop": "停損", "breakeven": "保本停損", "tp": "止盈", "time": "時間到", "opposite": "反向訊號",
             "external": "外部平倉", "flatten": "全部平倉", "stop_failed": "停損下單失敗", "stop_missing": "停損遺失",
             "open": "未結束（按現價）"}
SKIP_ZH = {"paused": "暫停中", "halted": "熔斷中", "stale": "過期", "bfbot_symbol": "bfbot 幣種",
           "not_on_demo": "測試網沒有", "foreign_position": "已有手動部位", "already_open": "已持有",
           "price_divergence": "測試網價差過大", "cap_open": "持倉數上限", "cap_side": "同方向上限",
           "too_small": "數量太小", "cap_gross": "總曝險上限", "cap_margin": "保證金不足",
           "leverage_error": "槓桿設定失敗"}


def notify(con, lines, force=False):
    """Batch notes; post at most about once an hour (right after the hourly scan) unless forced."""
    pending = json.loads(get_state(con, "pending_notes", "[]")) + lines
    if not pending:
        return
    if not force and not (4 <= time.gmtime().tm_min < 15) and not any(l.startswith(("⚠️", "🛑")) for l in lines):
        set_state(con, "pending_notes", json.dumps(pending[-60:], ensure_ascii=False))
        return
    try:
        hook = json.loads((QT / "desk" / "phone.json").read_text())["discord_webhook"]
        eq = con.execute("SELECT equity, open_n FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        foot = f"測試網權益 {eq[0]:,.0f} USDT，持倉 {eq[1]} 筆，策略 v{load_policy()['version']}" if eq else ""
        body = {"embeds": [{"title": "🤖 自動交易（測試網）", "description": "\n".join(pending)[:3900],
                            "color": 0x2B9DD9, "footer": {"text": foot}}]}
        req = urllib.request.Request(hook, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "User-Agent": "SignalDesk (local, 1.0)"})
        urllib.request.urlopen(req, timeout=20).read()
        set_state(con, "pending_notes", "[]")
    except Exception as e:
        set_state(con, "pending_notes", json.dumps(pending[-60:], ensure_ascii=False))
        event(con, "notify_error", repr(e)[:200])


def cmd_run(_a):
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{time.strftime('%F %T', time.gmtime())} auto: skipped, another run holds the lock")
            return
        api, con, pol = Api(), adb(), load_policy()
        r = Run(api, con, pol)
        r.record_equity()
        r.reconcile()
        r.exits()
        r.refresh()
        for tr in con.execute("SELECT * FROM trades WHERE status='closing'").fetchall():
            if r.position(tr) is None:
                try:
                    r.finalize(tr)
                except ApiError as e:
                    event(con, "error", f"finalize {tr['symbol']}: {e}")
        r.entries()
        con.commit()
        notify(con, r.notes)
        con.commit()
        n_open = con.execute("SELECT COUNT(*) FROM trades WHERE status IN ('open','closing')").fetchone()[0]
        print(f"{time.strftime('%F %T', time.gmtime())} auto: equity {r.snap['equity']:.2f} open {n_open} "
              f"notes {len(r.notes)}")


# ── reporting ──
def day_boot(rows, key, n=2000, rng=random.Random(0)):
    """Mean and 95% CI of `key`, bootstrapping whole UTC days."""
    by = {}
    for r in rows:
        by.setdefault(r["exit_time"] // DAY, []).append(r[key])
    days = list(by.values())
    vals = [v for d in days for v in d]
    if len(vals) < 2:
        return (vals[0] if vals else float("nan")), None
    means = []
    for _ in range(n):
        s = [v for d in (rng.choice(days) for _ in days) for v in d]
        means.append(sum(s) / len(s))
    means.sort()
    return sum(vals) / len(vals), (means[int(n * .025)], means[int(n * .975)])


def summ(rows, key="r_mult"):
    if not rows:
        return "n=0"
    v = sorted(r[key] for r in rows)
    m, ci = day_boot(rows, key)
    win = sum(1 for x in v if x > 0) / len(v)
    cis = f" CI95 [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""
    return f"n={len(v)} 勝率 {win:.0%} 平均 {m:+.2f}R 中位數 {v[len(v) // 2]:+.2f}R{cis}"


def cmd_report(a):
    con = adb()
    since = now_ms() - a.days * DAY if a.days else 0
    rows = [dict(r) for r in con.execute("SELECT * FROM trades WHERE status='closed' AND exit_time>=? "
                                         "AND r_mult IS NOT NULL ORDER BY exit_time", (since,))]
    allr = con.execute("SELECT status, note, COUNT(*) n FROM trades WHERE created_at>=? AND status!='test' "
                       "GROUP BY status, note",
                       (since,)).fetchall()
    print(f"== 已平倉（{'全部' if not a.days else f'近 {a.days} 天'}）")
    print("全部 ", summ(rows), f" 淨損益 {sum(r['net'] for r in rows):+.2f}U")
    # closed-only is biased toward early stop-outs (winners are still open): also show open trades at the mark
    try:
        pos = snapshot(Api())["pos"]
        live = []
        for t in con.execute("SELECT * FROM trades WHERE status IN ('open','closing') AND risk_usdt>0"):
            p = pos.get(t["symbol"])
            if p:
                live.append({"r_mult": float(p["unRealizedProfit"]) / t["risk_usdt"], "exit_time": now_ms()})
        if live:
            print(f"含未平倉（按現價，未扣出場手續費）：{summ(rows + live)}；其中持倉 {len(live)} 筆合計 "
                  f"{sum(x['r_mult'] for x in live):+.2f}R")
    except Exception as e:
        print(f"（讀不到持倉，略過含未平倉的統計：{e}）")
    for col, label in (("grade", "等級"), ("exit_reason", "出場"), ("side", "方向"), ("policy_version", "策略版本")):
        print(f"-- 依{label}")
        for k in sorted({r[col] for r in rows}, key=str):
            sub = [r for r in rows if r[col] == k]
            print(f"   {k!s:<12} {summ(sub)}  淨 {sum(r['net'] for r in sub):+.2f}U")
    if rows:
        def q(vals, p):
            vals = sorted(v for v in vals if v is not None)
            return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else float("nan")
        mfe, mae = [r["mfe_atr"] for r in rows], [r["mae_atr"] for r in rows]
        print(f"-- MFE（ATR）p25/50/75/90：{q(mfe, .25):.2f} / {q(mfe, .5):.2f} / {q(mfe, .75):.2f} / {q(mfe, .9):.2f}")
        print(f"-- MAE（ATR）p10/25/50/75：{q(mae, .1):.2f} / {q(mae, .25):.2f} / {q(mae, .5):.2f} / {q(mae, .75):.2f}")
        losers = [r for r in rows if r["r_mult"] <= 0 and r["mfe_atr"] is not None]
        for x in (0.5, 1, 2):
            print(f"   虧損單中曾經浮盈 ≥{x} ATR 的比例：{sum(1 for r in losers if r['mfe_atr'] >= x) / max(1, len(losers)):.0%}")
        slips = [r["slip_pct"] for r in rows if r["slip_pct"] is not None]
        print(f"-- 執行：平均滑價（相對訊號價，正=不利）{sum(slips) / max(1, len(slips)):+.3%}，"
              f"手續費 {sum(r['commission'] for r in rows):.2f}U，資金費 {sum(r['funding'] for r in rows):+.2f}U，"
              f"平均持有 {sum((r['exit_time'] - r['entry_time']) / HOUR for r in rows) / len(rows):.1f}h")
        paper_vs_live(rows)
    print("-- 推薦訊號的處理（含略過原因）")
    for r in allr:
        print(f"   {r['status']:<8} {SKIP_ZH.get(r['note'], r['note'] or ''):<12} {r['n']}")
    eq = con.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()
    if eq:
        peak, dd = 0, 0
        for _, e in eq:
            peak = max(peak, e)
            dd = max(dd, 1 - e / peak)
        print(f"-- 權益：起始 {eq[0][1]:,.2f} → 目前 {eq[-1][1]:,.2f} USDT（{eq[-1][1] / eq[0][1] - 1:+.2%}），"
              f"最大回撤 {dd:.2%}")


def paper_vs_live(rows):
    """Same signal, two executions: the ALLX paper book (signal price, 0.10%/side) vs this live testnet trade."""
    dk = desk()
    pairs = []
    for r in rows:
        p = dk.execute("SELECT entry_price, exit_price, exit_reason, pnl, qty FROM positions WHERE book='ALLX' AND "
                       "signal_id=? AND status='closed'", (r["signal_id"],)).fetchone()
        if p and r["notional"]:
            pairs.append((r, p))
    dk.close()
    if not pairs:
        print("-- 和紙上帳本 ALLX 對照：還沒有兩邊都平倉的訊號")
        return
    live = [1e4 * r["net"] / r["notional"] for r, _ in pairs]
    paper = [1e4 * p["pnl"] / (p["qty"] * p["entry_price"]) for _, p in pairs]
    entry = [1e4 * r["side"] * (p["entry_price"] - r["entry_price"]) / p["entry_price"] for r, p in pairs]
    exit_ = [1e4 * r["side"] * (r["exit_price"] - p["exit_price"]) / p["exit_price"] for r, p in pairs]
    same = sum(1 for r, p in pairs if {"stop": "stop", "opposite": "opposite_signal", "time": "time_stop"}
               .get(r["exit_reason"]) == p["exit_reason"])
    n = len(pairs)
    print(f"-- 和紙上帳本 ALLX 對照（同一訊號，n={n}）：實際 {sum(live) / n:+.1f}bp vs 紙上 {sum(paper) / n:+.1f}bp"
          f"（差 {(sum(live) - sum(paper)) / n:+.1f}bp）")
    print(f"   進場價差 {sum(entry) / n:+.1f}bp、出場價差 {sum(exit_) / n:+.1f}bp（正=實際較有利）；"
          f"出場原因相同 {same}/{n}。紙上成本固定每邊 0.10%，實際是真實手續費。")


def cmd_status(_a):
    api, con, pol = Api(), adb(), load_policy()
    snap = snapshot(api)
    print(f"權益 {snap['equity']:,.2f}（錢包 {snap['wallet']:,.2f}，未實現 {snap['upnl']:+.2f}），可用 {snap['avail']:,.2f}，"
          f"總曝險 {snap['gross']:,.0f}（{snap['gross'] / snap['equity']:.2f}x）")
    halt = int(get_state(con, "halt_until", 0))
    print(f"策略 v{pol['version']}；暫停={'是' if PAUSE.exists() else '否'}；熔斷="
          f"{'到 ' + iso(halt) if halt > now_ms() else '否'}")
    for tr in con.execute("SELECT * FROM trades WHERE status IN ('opening','open','closing') ORDER BY entry_time"):
        p = snap["pos"].get(tr["symbol"])
        up = float(p["unRealizedProfit"]) if p else 0
        print(f"  #{tr['id']} {tr['symbol']:<14} {'多' if tr['side'] > 0 else '空'} {tr['status']:<7} 進 {iso(tr['entry_time'])} "
              f"@{tr['entry_price'] or 0:.6g} 停損 {tr['stop_cur'] or 0:.6g} 風險 {tr['risk_usdt'] or 0:.1f}U "
              f"未實現 {up:+.2f}U ({up / tr['risk_usdt'] if tr['risk_usdt'] else 0:+.2f}R)")
    foreign = [s for s in snap["pos"] if not con.execute(
        f"SELECT 1 FROM trades WHERE symbol=? AND status IN {LIVE}", (s,)).fetchone()]
    if foreign:
        print("  非自動交易的部位（不會動它）：", ", ".join(foreign))


def cmd_policy(_a):
    con, pol = adb(), load_policy()
    print(json.dumps(pol, indent=2, ensure_ascii=False))
    print("範圍：", json.dumps({k: v for k, v in BOUNDS.items()}, ensure_ascii=False))
    print(f"規則：降低風險（{', '.join(sorted(DERISK))} 調低）隨時可改，每天最多 1 次（且需有上次修改後的新證據）；其他修改每天最多 1 次，且需距上次"
          f"修改 ≥{CHANGE_MIN_DAYS} 天、之後平倉 ≥{CHANGE_MIN_TRADES} 筆；數值每次最多變為 0.67–1.5 倍。")
    ok, why = change_allowed(con)
    print(f"目前可以做一般修改：{'是' if ok else '否'}（{why}）")
    for r in con.execute("SELECT * FROM policy_log ORDER BY ts"):
        print(f"  {iso(r['ts'])} v{r['version']} {r['kind']}: {r['changes']} — {r['reason']}")


def change_allowed(con):
    last = con.execute("SELECT MAX(ts) FROM policy_log WHERE kind='change'").fetchone()[0] or \
        int(get_state(con, "start_ms") or now_ms())
    n = con.execute("SELECT COUNT(*) FROM trades WHERE status='closed' AND entry_time>=?", (last,)).fetchone()[0]
    days = (now_ms() - last) / DAY
    today = con.execute("SELECT COUNT(*) FROM policy_log WHERE kind='change' AND ts>=?",
                        (now_ms() // DAY * DAY,)).fetchone()[0]
    ok = n >= CHANGE_MIN_TRADES and days >= CHANGE_MIN_DAYS and today == 0
    return ok, f"上次一般修改後 {days:.1f} 天、平倉 {n} 筆"


def parse_val(k, v):
    if k in NULLABLE and v.lower() in ("none", "null", "off"):
        return None
    if BOUNDS[k] is None:
        return v.lower() in ("1", "true", "yes", "on")
    return int(v) if k in INTS else float(v)


def cmd_set(a):
    con, pol = adb(), load_policy()
    override = os.environ.get("AUTO_OVERRIDE") == "1"     # the user at a shell; the tuning agent cannot set env vars
    if not a.reason or len(a.reason) < 20:
        raise SystemExit("拒絕：--reason 至少 20 字，要寫出依據（哪個報告、哪些數字）")
    new = dict(pol)
    changes = {}
    for kv in a.pairs:
        k, _, v = kv.partition("=")
        if k not in BOUNDS:
            raise SystemExit(f"拒絕：{k} 不是可調參數（{', '.join(BOUNDS)}）")
        val = parse_val(k, v)
        b = BOUNDS[k]
        if val is not None and b is not None and not (b[0] <= val <= b[1]):
            raise SystemExit(f"拒絕：{k}={val} 超出範圍 {b}")
        if val != pol[k]:
            changes[k] = (pol[k], val)
            new[k] = val
    if not changes:
        raise SystemExit("沒有變更")
    derisk = all(k in DERISK and v[1] is not None and v[0] is not None and v[1] < v[0] for k, v in changes.items())
    kind = "derisk" if derisk else "change"
    if not override:
        today = con.execute("SELECT COUNT(*) FROM policy_log WHERE kind=? AND ts>=?",
                            (kind, now_ms() // DAY * DAY)).fetchone()[0]
        if derisk and today >= 1:
            raise SystemExit("拒絕：今天已經降低過風險（每天最多 1 次）")
        if not derisk:
            ok, why = change_allowed(con)
            if not ok:
                raise SystemExit(f"拒絕：一般修改需距上次 ≥{CHANGE_MIN_DAYS} 天且平倉 ≥{CHANGE_MIN_TRADES} 筆，"
                                 f"每天最多 1 次（{why}）")
            for k, (old, val) in changes.items():
                if isinstance(old, (int, float)) and not isinstance(old, bool) and old and val is not None \
                        and not 0.67 <= val / old <= 1.5:
                    raise SystemExit(f"拒絕：{k} 一次最多變為 0.67–1.5 倍（{old} → {val}）")
    new["version"] = pol["version"] + 1
    if os.environ.get("AUTO_DRYRUN") == "1":          # autotune.py --dry-run: report, don't apply
        print(f"（試跑，不會套用）通過檢查，會更新為 v{new['version']}（{kind}）：{changes}")
        return
    save_policy(clamp_policy(new))
    con.execute("INSERT INTO policy_log VALUES(?,?,?,?,?)",
                (now_ms(), new["version"], kind + ("(user)" if override else ""),
                 json.dumps({k: v for k, v in changes.items()}, ensure_ascii=False), a.reason[:600]))
    con.commit()
    print(f"已更新為 v{new['version']}（{kind}）：{changes}")


def cmd_pause(_a):
    PAUSE.write_text(time.strftime("%F %T", time.gmtime()))
    print("已暫停開新單（停損、出場照常）")


def cmd_resume(_a):
    PAUSE.unlink(missing_ok=True)
    con = adb()
    set_state(con, "halt_until", 0)
    con.commit()
    print("已恢復（也解除熔斷）")


def cmd_flatten(_a):
    cmd_pause(_a)
    api, con, pol = Api(), adb(), load_policy()
    r = Run(api, con, pol)
    for tr in con.execute("SELECT * FROM trades WHERE status IN ('open','closing')").fetchall():
        p = r.position(tr)
        if p:
            market_close(api, con, tr, abs(float(p["positionAmt"])), "flatten")
    time.sleep(2)
    r.refresh()
    for tr in con.execute("SELECT * FROM trades WHERE status='closing'").fetchall():
        if r.position(tr) is None:
            r.finalize(tr)
    con.commit()
    notify(con, r.notes + ["已全部平倉並暫停"], force=True)
    con.commit()
    print("\n".join(r.notes) or "沒有持倉")


# ── whatif: replay a policy on every P1 pick ──
def path_for(con, sig):
    row = con.execute("SELECT complete, data, fetched FROM paths WHERE signal_id=?", (sig["id"],)).fetchone()
    # unfinished paths are refetched at most hourly: whatif shares the IP weight budget with the hourly scan
    if row and (row["complete"] or now_ms() - (row["fetched"] or 0) < HOUR):
        return json.loads(zlib.decompress(row["data"]))
    t0 = sig["registered_at"] - sig["registered_at"] % FIVE
    kl = public(PROD_PUBLIC, "/fapi/v1/klines", symbol=sig["symbol"], interval="5m", startTime=t0, limit=PATH_BARS)
    time.sleep(0.5)                              # klines limit 1500 = weight 10; stay far under 2400/min
    bars = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in kl if int(k[6]) <= now_ms()]
    complete = int(len(kl) >= PATH_BARS or t0 + PATH_BARS * FIVE < now_ms() - FIVE)
    con.execute("INSERT OR REPLACE INTO paths(signal_id, complete, data, fetched) VALUES(?,?,?,?)",
                (sig["id"], complete, zlib.compress(json.dumps(bars).encode()), now_ms()))
    return bars


def simulate(sig, f, bars, pol, opp_t):
    side, e, t_in = sig["side"], sig["price"], sig["registered_at"]
    atr = f["atr_pct"] * e
    stop = e - side * pol["sl_atr"] * atr
    tp = e + side * pol["tp_atr"] * atr if pol["tp_atr"] else None
    be_moved = False
    t_end = t_in + pol["max_hold_h"] * HOUR
    for t, o, h, l, _c in bars:
        if t + FIVE <= t_in:
            continue
        first = t <= t_in
        if opp_t and t >= opp_t:
            return o, "opposite", t
        if t >= t_end:
            return o, "time", t
        adverse, fav = (l, h) if side > 0 else (h, l)
        if side * (adverse - stop) <= 0:
            px = stop if first else (min(stop, o) if side > 0 else max(stop, o))
            return px, "breakeven" if be_moved else "stop", t
        if tp and side * (fav - tp) >= 0:
            return (tp if first else (max(tp, o) if side > 0 else min(tp, o))), "tp", t
        if pol["be_atr"] and not be_moved and side * (fav - e) >= pol["be_atr"] * atr:
            stop, be_moved = e * (1 + side * BE_BUFFER), True
    # not exited yet: mark at the last close. Dropping these would keep only the early stop-outs (survivor bias).
    return (bars[-1][4], "open", bars[-1][0]) if bars else None


def cmd_whatif(a):
    con = adb()
    day = str(now_ms() // DAY)
    used = json.loads(get_state(con, "whatif_count", "[0, 0]"))
    if used[0] != int(day):
        used = [int(day), 0]
    if used[1] >= WHATIF_PER_DAY and os.environ.get("AUTO_OVERRIDE") != "1":
        raise SystemExit(f"拒絕：whatif 每天最多 {WHATIF_PER_DAY} 次（避免在同一批資料上反覆調參）")
    set_state(con, "whatif_count", json.dumps([used[0], used[1] + 1]))
    con.commit()
    cur = load_policy()
    alt = dict(cur)
    for k, v in (("sl_atr", a.sl), ("tp_atr", a.tp), ("be_atr", a.be), ("max_hold_h", a.hold), ("opposite_exit", a.opp)):
        if v is not None:
            alt[k] = parse_val(k, v)
    alt = clamp_policy(alt)
    dk = desk()
    since = now_ms() - a.days * DAY
    sigs = dk.execute("SELECT id, symbol, side, price, features, pick_grade, registered_at FROM signals "
                      "WHERE user_pick=1 AND registered_at>=? ORDER BY registered_at", (since,)).fetchall()
    opp = {}
    for r in dk.execute("SELECT symbol, side, registered_at FROM signals WHERE registered_at>=? ORDER BY registered_at",
                        (since,)):
        opp.setdefault((r["symbol"], r["side"]), []).append(r["registered_at"])
    res = {"current": [], "alt": []}
    for s in sigs:
        f = json.loads(s["features"])
        if f["atr_pct"] <= 0:
            continue
        try:
            bars = path_for(con, s)
        except Exception:
            continue
        nxt = [t for t in opp.get((s["symbol"], -s["side"]), []) if t > s["registered_at"]]
        for name, pol in (("current", cur), ("alt", alt)):
            out = simulate(s, f, bars, pol, nxt[0] if (nxt and pol["opposite_exit"]) else None)
            if out is None:
                continue                        # no bars yet
            px, why, t = out
            ret = s["side"] * (px - s["price"]) / s["price"] - 2 * COST_SIDE
            res[name].append({"r_mult": ret / (pol["sl_atr"] * f["atr_pct"]), "bp": ret * 1e4, "exit_time": t,
                              "reason": why, "grade": s["pick_grade"], "t_in": s["registered_at"]})
    con.commit()
    span = f"{iso(sigs[0]['registered_at'])} ～ {iso(sigs[-1]['registered_at'])} UTC，實際約 " \
           f"{(sigs[-1]['registered_at'] - sigs[0]['registered_at']) / DAY:.1f} 天" if sigs else "無資料"
    print(f"推薦訊號 {len(sigs)} 個（{span}；查詢上限 {a.days} 天），進場用訊號價、成本每邊 {COST_SIDE:.2%}、不含資金費")
    for name, pol in (("目前策略", cur), ("比較策略", alt)):
        rows = res["current" if name == "目前策略" else "alt"]
        print(f"== {name}：sl {pol['sl_atr']} ATR，tp {pol['tp_atr']}，保本 {pol['be_atr']}，"
              f"持有上限 {pol['max_hold_h']}h，反向出場 {pol['opposite_exit']}")
        print("   ", summ(rows), f"平均 {sum(r['bp'] for r in rows) / max(1, len(rows)):+.1f}bp")
        if rows:
            mid = sorted(r["t_in"] for r in rows)[len(rows) // 2]
            h1, h2 = [r for r in rows if r["t_in"] < mid], [r for r in rows if r["t_in"] >= mid]
            print(f"    前半 {summ(h1)}\n    後半 {summ(h2)}")
            mix = {}
            for r in rows:
                mix[r["reason"]] = mix.get(r["reason"], 0) + 1
            print("    出場：", "、".join(f"{REASON_ZH.get(k, k)} {v}" for k, v in sorted(mix.items())))
            eq, peak, dd = 1.0, 1.0, 0.0
            for r in sorted(rows, key=lambda r: r["exit_time"]):
                mult = pol["risk_mult_A"] if r["grade"] == "A" else pol["risk_mult_B"]
                eq *= 1 + pol["risk_pct"] / 100 * mult * r["r_mult"]
                peak = max(peak, eq)
                dd = max(dd, 1 - eq / peak)
            print(f"    以每筆風險 {pol['risk_pct']}% 依序複利（不計持倉上限）：{eq - 1:+.2%}，最大回撤 {dd:.2%}")
    print(f"今天 whatif 已用 {used[1] + 1}/{WHATIF_PER_DAY} 次。小樣本差異大多是雜訊，看 CI 和前後半是否一致。")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("report")
    p.add_argument("--days", type=int, default=0)
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("whatif")
    for k in ("sl", "tp", "be", "hold", "opp"):
        p.add_argument(f"--{k}")
    p.add_argument("--days", type=int, default=60)
    p.set_defaults(fn=cmd_whatif)
    sub.add_parser("policy").set_defaults(fn=cmd_policy)
    p = sub.add_parser("set")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_set)
    for name, fn in (("pause", cmd_pause), ("resume", cmd_resume), ("flatten", cmd_flatten)):
        sub.add_parser(name).set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
