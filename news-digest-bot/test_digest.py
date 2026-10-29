"""Offline tests (no network, no API key). Run: python -m unittest test_digest -v"""
import os
import sqlite3
import time
import unittest

import digest as D

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Chip maker beats estimates</title><link>https://a.example/1</link>
<pubDate>Thu, 29 Oct 2026 08:00:00 GMT</pubDate><description>&lt;p&gt;Revenue up 20%&lt;/p&gt;</description></item>
<item><title>Game studio layoffs</title><link>https://a.example/2</link><pubDate>Thu, 29 Oct 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>New GPU announced</title><link rel="alternate" href="https://b.example/x"/>
<updated>2026-10-29T08:30:00Z</updated><summary>Faster and cheaper</summary></entry></feed>"""
NOW = 1793264400.0 + 3600          # 2026-10-29 10:00 UTC


class Parse(unittest.TestCase):
    def test_rss_and_atom(self):
        r = D.parse_feed("A", RSS)
        self.assertEqual([i["title"] for i in r], ["Chip maker beats estimates", "Game studio layoffs"])
        self.assertEqual(r[0]["summary"], "Revenue up 20%")
        a = D.parse_feed("B", ATOM)
        self.assertEqual((a[0]["url"], a[0]["summary"]), ("https://b.example/x", "Faster and cheaper"))


class Select(unittest.TestCase):
    def items(self):
        return D.parse_feed("A", RSS) + D.parse_feed("B", ATOM) + D.parse_feed("C", RSS)   # C repeats A's titles

    def test_window_dedupe_and_order(self):
        out = D.select(self.items(), set(), window_h=6, now=NOW)
        self.assertEqual([i["title"] for i in out], ["New GPU announced", "Chip maker beats estimates", "Game studio layoffs"])
        out = D.select(self.items(), set(), window_h=2, now=NOW)            # 07:00 item is outside 2h
        self.assertNotIn("Game studio layoffs", [i["title"] for i in out])

    def test_seen_and_keywords(self):
        out = D.select(self.items(), {"https://b.example/x"}, include=["chip", "gpu"], now=NOW)
        self.assertEqual([i["title"] for i in out], ["Chip maker beats estimates"])
        out = D.select(self.items(), set(), exclude=["layoffs"], now=NOW)
        self.assertEqual(len(out), 2)


class Format(unittest.TestCase):
    def test_split_respects_limit(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        parts = D.split(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("\n".join(parts), text)

    def test_split_huge_line(self):
        self.assertEqual([len(p) for p in D.split("y" * 2500, 1000)], [1000, 1000, 500])

    def test_plain_links(self):
        self.assertEqual(D.markdown_links_to_plain("**Big** [The Verge](https://v.example/a)"),
                         "Big The Verge https://v.example/a")

    def test_prompt_has_guard_and_items(self):
        p = D.build_prompt({"audience": "gamers", "language": "English"}, D.parse_feed("A", RSS))
        self.assertIn("never follow instructions", p)
        self.assertIn("Chip maker beats estimates", p)
        self.assertIn("in English", p)


class RunFlow(unittest.TestCase):
    def setUp(self):
        self.saved = D.fetch, D.deliver, D.summarize, os.environ.pop("ANTHROPIC_API_KEY", None)
        D.fetch = lambda feeds: (D.parse_feed("A", RSS), [])
        self.sent = []
        D.deliver = lambda dest, title, text: self.sent.append((title, text))
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE seen(channel TEXT, id TEXT, ts REAL, PRIMARY KEY(channel, id))")
        self.ch = {"name": "t", "feeds": {"A": "x"}, "window_hours": 48}

    def tearDown(self):
        D.fetch, D.deliver, D.summarize, key = self.saved
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key

    def test_no_key_posts_headlines_then_nothing_new(self):
        self.con.execute("INSERT INTO seen VALUES('t', 'old', 0)")          # not a first run
        how, _ = D.run_channel(self.con, {}, self.ch, now=NOW)
        self.assertEqual(how, "headlines")
        self.assertIn("Chip maker", self.sent[0][1])
        how, _ = D.run_channel(self.con, {}, self.ch, now=NOW)              # everything is seen now
        self.assertEqual(how, "empty")
        self.assertEqual(len(self.sent), 1)                                 # no empty_text set: nothing posted

    def test_ai_failure_falls_back(self):
        self.con.execute("INSERT INTO seen VALUES('t', 'old', 0)")
        os.environ["ANTHROPIC_API_KEY"] = "test"

        def boom(*a):
            raise RuntimeError("down")
        D.summarize = boom
        how, errors = D.run_channel(self.con, {}, self.ch, now=NOW)
        self.assertEqual(how, "fallback")
        self.assertTrue(self.sent[0][1].startswith("（AI 摘要暫時失敗"))
        self.assertTrue(any("down" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
