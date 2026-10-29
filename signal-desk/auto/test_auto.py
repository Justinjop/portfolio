"""Unit tests for auto.py's pure logic (no network, no keys). Run: ~/venv/bin/python -m unittest test_auto -v"""
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto as A  # noqa: E402

H = A.HOUR


def bars(t0, rows):
    """rows of (o, h, l, c) -> 5m bars starting at t0."""
    return [[t0 + i * A.FIVE, o, h, l, c] for i, (o, h, l, c) in enumerate(rows)]


class Rounding(unittest.TestCase):
    def test_step_rounds_down(self):
        self.assertEqual(A.round_step(6.1999, "0.01"), Decimal("6.19"))
        self.assertEqual(A.round_step(86820.7, "1"), Decimal("86820"))

    def test_tick_rounds_nearest(self):
        self.assertEqual(A.round_tick(97.9314, "0.01"), Decimal("97.93"))
        self.assertEqual(A.fmt(A.round_tick(0.0049786, "0.0000001")), "0.0049786")


class Policy(unittest.TestCase):
    def test_clamp(self):
        p = A.clamp_policy({"risk_pct": 9, "sl_atr": 0.1, "tp_atr": None, "max_open": 7.6, "opposite_exit": 0,
                            "junk": 1})
        self.assertEqual(p["risk_pct"], 1.0)
        self.assertEqual(p["sl_atr"], 1.0)
        self.assertIsNone(p["tp_atr"])
        self.assertEqual(p["max_open"], 8)
        self.assertIs(p["opposite_exit"], False)
        self.assertNotIn("junk", p)

    def test_defaults_inside_bounds(self):
        self.assertEqual(A.clamp_policy(A.DEFAULT_POLICY), A.DEFAULT_POLICY)


class SetRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.saved = A.DB, A.POLICY
        A.DB, A.POLICY = d / "a.sqlite", d / "policy.json"
        os.environ.pop("AUTO_OVERRIDE", None)
        con = A.adb()
        A.set_state(con, "start_ms", A.now_ms())
        con.commit()

    def tearDown(self):
        A.DB, A.POLICY = self.saved
        self.tmp.cleanup()

    def set(self, *pairs, reason="a reason that is long enough to pass"):
        A.cmd_set(types.SimpleNamespace(pairs=list(pairs), reason=reason))

    def test_derisk_allowed_immediately(self):
        self.set("risk_pct=0.2")
        self.assertEqual(A.load_policy()["risk_pct"], 0.2)
        self.assertEqual(A.load_policy()["version"], 2)

    def test_raise_refused_without_cadence(self):
        with self.assertRaises(SystemExit):
            self.set("risk_pct=0.3")
        with self.assertRaises(SystemExit):
            self.set("be_atr=1.5")           # exit-rule change is a general change too
        self.assertEqual(A.load_policy()["version"], 1)

    def test_out_of_bounds_and_short_reason(self):
        with self.assertRaises(SystemExit):
            self.set("risk_pct=0.01")
        with self.assertRaises(SystemExit):
            self.set("risk_pct=0.2", reason="short")
        with self.assertRaises(SystemExit):
            self.set("leverage=20")

    def test_derisk_limit_per_day(self):
        for v in ("0.24",):
            self.set(f"risk_pct={v}")
        with self.assertRaises(SystemExit):
            self.set("risk_pct=0.23")

    def test_general_change_after_cadence_and_step_limit(self):
        con = A.adb()
        old = A.now_ms() - 8 * A.DAY
        A.set_state(con, "start_ms", old)
        for i in range(30):
            con.execute("INSERT INTO trades(signal_id, status, entry_time, exit_time) VALUES(?,?,?,?)",
                        (i, "closed", old + 1000, old + 2000))
        con.commit()
        with self.assertRaises(SystemExit):
            self.set("risk_pct=0.5")         # 2x > 1.5x step
        self.set("risk_pct=0.3")
        self.assertEqual(A.load_policy()["risk_pct"], 0.3)
        with self.assertRaises(SystemExit):
            self.set("sl_atr=2.0")           # second general change the same day / no new trades

    def test_dryrun_does_not_apply(self):
        os.environ["AUTO_DRYRUN"] = "1"
        try:
            self.set("risk_pct=0.2")
        finally:
            os.environ.pop("AUTO_DRYRUN")
        self.assertEqual(A.load_policy()["version"], 1)
        self.assertEqual(A.adb().execute("SELECT COUNT(*) FROM policy_log").fetchone()[0], 0)

    def test_user_override(self):
        os.environ["AUTO_OVERRIDE"] = "1"
        try:
            self.set("risk_pct=0.8")
        finally:
            os.environ.pop("AUTO_OVERRIDE")
        self.assertEqual(A.load_policy()["risk_pct"], 0.8)


class Simulate(unittest.TestCase):
    pol = dict(A.DEFAULT_POLICY)
    t0 = 1_000 * A.FIVE

    def sig(self, side=1):
        return {"side": side, "price": 100.0, "registered_at": self.t0 + 10}

    def test_long_stop(self):
        f = {"atr_pct": 0.01}                          # stop at 97.5
        b = bars(self.t0, [(100, 101, 99, 100), (100, 100, 97, 97.2)])
        px, why, _ = A.simulate(self.sig(), f, b, self.pol, None)
        self.assertEqual((px, why), (97.5, "stop"))

    def test_short_tp_and_gap(self):
        pol = dict(self.pol, tp_atr=2.0)              # short tp at 98, stop 102.5
        b = bars(self.t0, [(100, 100.5, 99.5, 100), (97.0, 97.5, 96.0, 96.5)])
        px, why, _ = A.simulate(self.sig(-1), {"atr_pct": 0.01}, b, pol, None)
        self.assertEqual((px, why), (97.0, "tp"))      # gapped through: filled at the open

    def test_breakeven(self):
        pol = dict(self.pol, be_atr=1.0)
        b = bars(self.t0, [(100, 101.2, 99.8, 101), (101, 101, 99.5, 99.6)])
        px, why, _ = A.simulate(self.sig(), {"atr_pct": 0.01}, b, pol, None)
        self.assertEqual(why, "breakeven")
        self.assertAlmostEqual(px, 100.1)

    def test_opposite_and_time(self):
        b = bars(self.t0, [(100, 100.5, 99.5, 100)] * 30)
        _, why, _ = A.simulate(self.sig(), {"atr_pct": 0.01}, b, self.pol, self.t0 + 5 * A.FIVE)
        self.assertEqual(why, "opposite")
        _, why, _ = A.simulate(self.sig(), {"atr_pct": 0.01}, b, dict(self.pol, max_hold_h=1), None)
        self.assertEqual(why, "time")

    def test_unfinished_marked_to_market(self):
        b = bars(self.t0, [(100, 100.5, 99.5, 100.3)] * 3)
        px, why, _ = A.simulate(self.sig(), {"atr_pct": 0.01}, b, self.pol, None)
        self.assertEqual((px, why), (100.3, "open"))


class Gates(unittest.TestCase):
    def run_obj(self, equity=5000.0, gross=0.0, avail=5000.0):
        r = A.Run.__new__(A.Run)
        r.pol = dict(A.DEFAULT_POLICY)
        r.snap = {"equity": equity, "gross": gross, "avail": avail, "pos": {}, "orders": [], "algos": []}
        return r

    def sig(self, sym="SOLUSDT", side=1, price=100.0, age=0):
        return {"id": 1, "symbol": sym, "side": side, "price": price, "pick_grade": "A",
                "registered_at": A.now_ms() - age}

    fl = {"SOLUSDT": {"trading": True, "tick": "0.01", "step": "0.01", "min_qty": "0.01", "max_qty": 1e6,
                      "min_notional": 5}}

    def gate(self, s, marks=None, foreign=(), live=None, halted=False, paused=False):
        return self.run_obj().gate(s, {"atr_pct": 0.01}, marks or {"SOLUSDT": 100.0, "BTCUSDT": 1.0}, self.fl,
                                   set(foreign), live or {}, halted, paused)

    def test_gates(self):
        self.assertIsNone(self.gate(self.sig()))
        self.assertEqual(self.gate(self.sig(), paused=True), "paused")
        self.assertEqual(self.gate(self.sig(), halted=True), "halted")
        self.assertEqual(self.gate(self.sig(age=3 * H)), "stale")
        self.assertEqual(self.gate(self.sig("BTCUSDT")), "bfbot_symbol")
        self.assertEqual(self.gate(self.sig("XYZUSDT")), "not_on_demo")
        self.assertEqual(self.gate(self.sig(), foreign={"SOLUSDT"}), "foreign_position")
        self.assertEqual(self.gate(self.sig(), live={"SOLUSDT": 1}), "already_open")
        self.assertEqual(self.gate(self.sig(price=97.0)), "price_divergence")
        live = {f"S{i}": 1 for i in range(30)}
        self.assertEqual(self.gate(self.sig(), live=live), "cap_open")
        live = {f"S{i}": 1 for i in range(20)}
        self.assertEqual(self.gate(self.sig(), live=live), "cap_side")

    def test_size_risk_and_caps(self):
        r = self.run_obj()
        q, risk = r.size(self.sig(), {"atr_pct": 0.01}, 100.0, self.fl["SOLUSDT"])
        self.assertAlmostEqual(risk, 12.5, delta=0.01)          # 0.25% of 5000; stop 2.5 USDT -> 5 SOL
        self.assertEqual(q, Decimal("5.00"))
        q, risk = r.size(self.sig(), {"atr_pct": 0.001}, 100.0, self.fl["SOLUSDT"])
        self.assertEqual(q, Decimal("12.50"))                   # capped at 25% notional = 1250 USDT
        self.assertLess(risk, 12.5)


class FakeApi:
    def __init__(self, algo_fail=False, algo_status=None, fills=()):
        self.calls, self.algo_fail, self.algo_status, self.fills = [], algo_fail, algo_status or {}, list(fills)

    def req(self, method, path, signed=True, **p):
        self.calls.append((method, path, p))
        if method == "POST" and path == "/fapi/v1/algoOrder" and self.algo_fail:
            raise A.ApiError(400, '{"code":-2021,"msg":"Order would immediately trigger."}', path)
        if method == "GET" and path == "/fapi/v1/algoOrder":
            return {"algoStatus": self.algo_status.get(p["clientAlgoId"], "NEW")}
        if path == "/fapi/v1/userTrades":
            return self.fills
        if path == "/fapi/v1/income":
            return []
        return {}


class ExecutorPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = A.DB, A.demo_klines
        A.DB = Path(self.tmp.name) / "a.sqlite"
        A.demo_klines = lambda *a, **k: []
        self.con = A.adb()
        self.con.execute("INSERT INTO trades(id, signal_id, symbol, side, grade, status, created_at, sig_price, atr_pct,"
                         " entry_order, entry_time, entry_price, qty, notional, risk_usdt, stop_init, stop_cur, stop_n)"
                         " VALUES(5, 9, 'SOLUSDT', 1, 'A', 'open', 0, 100, 0.01, 77, 1000, 100, 5, 500, 12.5, 97.5, 97.5, 2)")
        self.con.commit()

    def tearDown(self):
        A.DB, A.demo_klines = self.saved
        self.tmp.cleanup()

    def run_obj(self, api, pos_amt="5", mark="99"):
        r = A.Run.__new__(A.Run)
        r.api, r.con, r.pol, r.notes = api, self.con, dict(A.DEFAULT_POLICY), []
        r.fl = {"SOLUSDT": {"trading": True, "tick": "0.01", "step": "0.01", "min_qty": "0.01", "max_qty": 1e6,
                            "min_notional": 5}}
        r.snap = {"pos": {"SOLUSDT": {"positionAmt": pos_amt, "markPrice": mark}} if pos_amt else {},
                  "algos": [], "orders": [], "equity": 5000, "gross": 0, "avail": 5000}
        return r

    def trade(self):
        return self.con.execute("SELECT * FROM trades WHERE id=5").fetchone()

    def test_missing_stop_that_cannot_be_replaced_is_closed(self):
        api = FakeApi(algo_fail=True)
        r = self.run_obj(api)
        r.protect(self.trade())
        closes = [c for c in api.calls if c[1] == "/fapi/v1/order" and c[2].get("reduceOnly") == "true"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][2]["side"], "SELL")
        self.assertEqual(self.trade()["status"], "closing")
        self.assertEqual(self.trade()["exit_reason"], "stop_missing")
        self.assertTrue(r.notes and r.notes[0].startswith("⚠️"))

    def test_missing_stop_is_replaced(self):
        api = FakeApi()
        self.run_obj(api).protect(self.trade())
        placed = [c for c in api.calls if c[1] == "/fapi/v1/algoOrder" and c[0] == "POST"]
        self.assertEqual(placed[0][2]["clientAlgoId"], "dsk-5-s3")
        self.assertEqual(placed[0][2]["triggerPrice"], "97.5")
        self.assertEqual(self.trade()["status"], "open")

    def test_exit_reason_checks_older_stops(self):
        fills = [{"side": "SELL", "time": 2000, "qty": "5", "price": "97.4", "realizedPnl": "-13", "orderId": 88,
                  "commission": "0.2", "commissionAsset": "USDT"}]
        api = FakeApi(algo_status={"dsk-5-s1": "FINISHED", "dsk-5-s2": "NEW"}, fills=fills)
        r = self.run_obj(api, pos_amt=None)
        r.finalize(self.trade())
        t = self.trade()
        self.assertEqual((t["status"], t["exit_reason"]), ("closed", "stop"))
        self.assertAlmostEqual(t["net"], -13.2)
        self.assertAlmostEqual(t["r_mult"], -13.2 / 12.5)


class PaperVsLive(unittest.TestCase):
    def test_pairs(self):
        import contextlib
        import io
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        mem.execute("CREATE TABLE positions(book TEXT, signal_id INTEGER, entry_price REAL, exit_price REAL, "
                    "exit_reason TEXT, pnl REAL, qty REAL, status TEXT)")
        # paper: short 10 @100 -> 98, pnl after costs 18
        mem.execute("INSERT INTO positions VALUES('ALLX', 7, 100, 98, 'stop', 18, 10, 'closed')")
        saved = A.desk
        A.desk = lambda: mem
        rows = [{"signal_id": 7, "side": -1, "entry_price": 99.9, "exit_price": 98.2, "exit_reason": "stop",
                 "net": 16.5, "notional": 999.0}]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                A.paper_vs_live(rows)
        finally:
            A.desk = saved
        text = out.getvalue()
        self.assertIn("n=1", text)
        self.assertIn("實際 +165.2bp vs 紙上 +180.0bp", text)
        self.assertIn("進場價差 -10.0bp", text)        # short filled 0.1% lower than paper: worse
        self.assertIn("出場原因相同 1/1", text)


if __name__ == "__main__":
    unittest.main()
