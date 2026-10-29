"""Offline tests (no network). Run: python -m unittest test_watch -v"""
import tempfile
import unittest
from pathlib import Path

import watch as W


class Parsers(unittest.TestCase):
    def test_steam(self):
        data = {"1091500": {"success": True, "data": {"name": "Cyberpunk 2077", "price_overview": {
            "currency": "TWD", "initial": 129900, "final": 64900, "discount_percent": 50}}}}
        self.assertEqual(W.parse_steam("1091500", data),
                         (649.0, "TWD", "Cyberpunk 2077", {"discount": 50, "initial": 1299.0}))

    def test_steam_free_and_missing(self):
        free = {"570": {"success": True, "data": {"name": "Dota 2", "is_free": True}}}
        self.assertEqual(W.parse_steam("570", free)[0], None)
        with self.assertRaises(ValueError):
            W.parse_steam("1", {"1": {"success": False}})

    def test_fx(self):
        self.assertEqual(W.parse_fx("USD", "TWD", {"result": "success", "rates": {"TWD": 32.1}})[0], 32.1)
        with self.assertRaises(ValueError):
            W.parse_fx("USD", "XXX", {"result": "success", "rates": {}})

    def test_numbers(self):
        self.assertEqual(W.parse_number("NT$1,299"), 1299.0)
        self.assertEqual(W.parse_number("1.299,50 €"), 1299.5)
        self.assertEqual(W.parse_number("$12.99"), 12.99)

    def test_page(self):
        html = '<span class="price">NT$ 2,490</span>'
        self.assertEqual(W.parse_page(html, r'class="price">([^<]+)<')[0], 2490.0)
        with self.assertRaises(ValueError):
            W.parse_page("<p>sold out</p>", r'class="price">([^<]+)<')


class Rules(unittest.TestCase):
    w = {"below": 500.0, "above": None, "drop_pct": 40.0, "last_alert": None}

    def test_evaluate(self):
        self.assertEqual(W.evaluate(self.w, 600, 700), [])
        self.assertEqual(len(W.evaluate(self.w, 400, 1000)), 2)      # below + 60% drop
        self.assertEqual(W.evaluate(self.w, None, 1000), [])

    def test_alert_once_then_on_new_low(self):
        hits = W.evaluate(self.w, 480, 600)
        ok, state = W.should_alert(self.w, 480, hits)
        self.assertTrue(ok)
        w2 = dict(self.w, last_alert=state)
        self.assertFalse(W.should_alert(w2, 480, hits)[0])          # same price: no repeat
        self.assertFalse(W.should_alert(w2, 490, W.evaluate(w2, 490, 600))[0])
        self.assertTrue(W.should_alert(w2, 450, W.evaluate(w2, 450, 600))[0])   # new low
        self.assertTrue(W.should_alert(w2, 300, W.evaluate(w2, 300, 600))[0])   # a new rule (drop) starts hitting


class CheckFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = W.DB, W.FETCH, W.notify
        W.DB = Path(self.tmp.name) / "t.sqlite"
        self.prices = iter([700, 480, 480, 520, 470])
        W.FETCH = {"steam": lambda w: (next(self.prices), "TWD", "Game", {})}
        self.sent = []
        W.notify = lambda cfg, title, text, url=None: self.sent.append(title)

    def tearDown(self):
        W.DB, W.FETCH, W.notify = self.saved
        self.tmp.cleanup()

    def test_sequence(self):
        con = W.db()
        con.execute("INSERT INTO watches(kind, target, below) VALUES('steam', '1', 500)")
        alerts = []
        for t in range(5):
            w = con.execute("SELECT * FROM watches").fetchone()
            alerts.append(W.check_one(con, {}, w, now=1000 + t)[2])
            con.commit()
        # 700 no, 480 alert, 480 quiet, 520 clears (re-arm), 470 alert again
        self.assertEqual(alerts, [False, True, False, False, True])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(con.execute("SELECT name FROM watches").fetchone()[0], "Game")
        con.close()


if __name__ == "__main__":
    unittest.main()
