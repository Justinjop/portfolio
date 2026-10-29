"""price-watch: get a Discord / Telegram message when a price hits your target.

Sources
  steam <appid>              Steam store price (region via --cc, default tw)
  fx <BASE> <QUOTE>          exchange rate, e.g. fx USD TWD
  page <url> --regex RE      any page; RE's first group is the price ("1,299" and "NT$1299" both work)

Rules (any combination; a watch alerts once per new low / state change, not every run)
  --below X                  price <= X
  --drop P                   price is P% or more under the highest price of the last 30 days
  --above X                  price >= X (useful for fx)

python watch.py add steam 1091500 --below 400 --drop 40
python watch.py add fx USD TWD --below 31.5
python watch.py add page https://example.com/item --regex "price\\":\\s*([\\d.]+)" --below 999 --name "Example item"
python watch.py list | check | history ID | remove ID
Run `check` from cron / Task Scheduler / systemd every 30-60 minutes.
Config: config.json next to this file: {"discord_webhook": "...", "telegram_token": "...", "telegram_chat": "..."}
Standard library only (Python 3.10+).
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "watch.sqlite"
CONFIG = HERE / "config.json"
UA = "price-watch/1.0 (personal price alerts)"
DAY = 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS watches(id INTEGER PRIMARY KEY, kind TEXT, target TEXT, name TEXT, regex TEXT, cc TEXT,
    below REAL, above REAL, drop_pct REAL, last_alert TEXT, created REAL);
CREATE TABLE IF NOT EXISTS prices(watch_id INTEGER, ts REAL, price REAL, currency TEXT, extra TEXT);
"""


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def http_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        return json.loads(r.read().decode())


def http_text(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


# ── sources: each returns (price, currency, name, extra) ──
def parse_steam(appid, data):
    entry = data.get(str(appid)) or {}
    if not entry.get("success"):
        raise ValueError(f"steam app {appid} not found")
    d = entry["data"]
    po = d.get("price_overview")
    if not po:                                   # free or not sold in this region
        return None, None, d.get("name"), {"free": d.get("is_free", False)}
    return po["final"] / 100, po["currency"], d.get("name"), {"discount": po.get("discount_percent", 0),
                                                              "initial": po["initial"] / 100}


def fetch_steam(w):
    q = urllib.parse.urlencode({"appids": w["target"], "cc": w["cc"] or "tw", "filters": "basic,price_overview"})
    return parse_steam(w["target"], http_json(f"https://store.steampowered.com/api/appdetails?{q}"))


def parse_fx(base, quote, data):
    if data.get("result") != "success" or quote not in data.get("rates", {}):
        raise ValueError(f"rate {base}/{quote} unavailable")
    return float(data["rates"][quote]), quote, f"{base}/{quote}", {}


def fetch_fx(w):
    base, quote = w["target"].split("/")
    return parse_fx(base, quote, http_json(f"https://open.er-api.com/v6/latest/{base}"))


def parse_number(s):
    s = re.sub(r"[^\d.,]", "", s)
    if "," in s and "." in s and s.rfind(",") > s.rfind("."):   # 1.299,00 (European)
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return float(s)


def parse_page(html, regex):
    m = re.search(regex, html, re.S)
    if not m:
        raise ValueError("regex did not match: the page changed or the price is not in the HTML")
    return parse_number(m.group(1)), None, None, {}


def fetch_page(w):
    return parse_page(http_text(w["target"]), w["regex"])


FETCH = {"steam": fetch_steam, "fx": fetch_fx, "page": fetch_page}


# ── rules ──
def evaluate(w, price, peak30):
    """Return the list of rule hits (strings) for this price."""
    hits = []
    if price is None:
        return hits
    if w["below"] is not None and price <= w["below"]:
        hits.append(f"≤ {w['below']:g}")
    if w["above"] is not None and price >= w["above"]:
        hits.append(f"≥ {w['above']:g}")
    if w["drop_pct"] and peak30 and price <= peak30 * (1 - w["drop_pct"] / 100):
        hits.append(f"比 30 天高點 {peak30:g} 低 {1 - price / peak30:.0%}")
    return hits


def should_alert(w, price, hits):
    """Alert on a new rule state, or a lower price than the last alert while still hit (no repeats every run)."""
    if not hits:
        return False, None
    state = json.dumps({"hits": sorted(h.split(" ")[0] for h in hits), "price": price})
    last = json.loads(w["last_alert"]) if w["last_alert"] else None
    if last is None or sorted(last["hits"]) != sorted(h.split(" ")[0] for h in hits):
        return True, state
    lower_is_news = w["above"] is None
    if (price < last["price"]) if lower_is_news else (price > last["price"]):
        return True, state
    return False, None


# ── notify ──
def notify(cfg, title, text, url=None):
    sent = []
    if cfg.get("discord_webhook"):
        body = {"embeds": [{"title": title[:250], "description": text[:3900], "color": 0x3BA55D,
                            **({"url": url} if url else {})}]}
        urllib.request.urlopen(urllib.request.Request(cfg["discord_webhook"], data=json.dumps(body).encode(),
                                                      headers={"Content-Type": "application/json", "User-Agent": UA}),
                               timeout=20).read()
        sent.append("discord")
    if cfg.get("telegram_token") and cfg.get("telegram_chat"):
        q = urllib.parse.urlencode({"chat_id": cfg["telegram_chat"], "text": f"{title}\n{text}" + (f"\n{url}" if url else "")})
        urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage?{q}", timeout=20).read()
        sent.append("telegram")
    return sent


def link(w):
    if w["kind"] == "steam":
        return f"https://store.steampowered.com/app/{w['target']}/"
    return w["target"] if w["kind"] == "page" else None


# ── commands ──
def cmd_add(a):
    con = db()
    if a.kind == "steam":
        target = str(int(a.target[0]))
    elif a.kind == "fx":
        if len(a.target) != 2:
            raise SystemExit("fx needs BASE QUOTE, e.g. fx USD TWD")
        target = f"{a.target[0].upper()}/{a.target[1].upper()}"
    else:
        if not a.regex or "(" not in a.regex:
            raise SystemExit("page needs --regex with one capture group for the price")
        re.compile(a.regex)
        target = a.target[0]
    if a.below is None and a.above is None and a.drop is None:
        raise SystemExit("give at least one rule: --below, --above or --drop")
    cur = con.execute("INSERT INTO watches(kind, target, name, regex, cc, below, above, drop_pct, created) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (a.kind, target, a.name, a.regex, a.cc, a.below, a.above, a.drop, time.time()))
    con.commit()
    print(f"added #{cur.lastrowid} {a.kind} {target}")


def check_one(con, cfg, w, now=None):
    now = now or time.time()
    price, cur, name, extra = FETCH[w["kind"]](w)
    if name and not w["name"]:
        con.execute("UPDATE watches SET name=? WHERE id=?", (name, w["id"]))
    con.execute("INSERT INTO prices VALUES(?,?,?,?,?)", (w["id"], now, price, cur, json.dumps(extra)))
    peak = con.execute("SELECT MAX(price) FROM prices WHERE watch_id=? AND ts>=?", (w["id"], now - 30 * DAY)).fetchone()[0]
    hits = evaluate(w, price, peak)
    alert, state = should_alert(w, price, hits)
    if alert:
        label = w["name"] or name or w["target"]
        disc = f"（-{extra['discount']}%，原價 {extra['initial']:g}）" if extra.get("discount") else ""
        notify(cfg, f"🔔 {label}：{price:g} {cur or ''}".strip(), f"{'、'.join(hits)}{disc}", link(w))
        con.execute("UPDATE watches SET last_alert=? WHERE id=?", (state, w["id"]))
    elif not hits and w["last_alert"]:
        con.execute("UPDATE watches SET last_alert=NULL WHERE id=?", (w["id"],))   # re-arm once the rule clears
    return price, hits, alert


def cmd_check(_a):
    con = db()
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    errors = 0
    for w in con.execute("SELECT * FROM watches ORDER BY id").fetchall():
        try:
            price, hits, alert = check_one(con, cfg, w)
            print(f"#{w['id']} {w['name'] or w['target']}: {price} {'ALERT ' if alert else ''}{', '.join(hits)}")
        except Exception as e:                     # one broken source must not stop the others
            errors += 1
            print(f"#{w['id']} {w['target']}: ERROR {e}", file=sys.stderr)
        con.commit()
        time.sleep(1.5)                            # be polite to the sources
    return errors


def cmd_list(_a):
    for w in db().execute("SELECT w.*, (SELECT price FROM prices p WHERE p.watch_id=w.id ORDER BY ts DESC LIMIT 1) last "
                          "FROM watches w ORDER BY id"):
        rules = " ".join(f"{k}={w[c]:g}" for k, c in (("below", "below"), ("above", "above"), ("drop%", "drop_pct"))
                         if w[c] is not None)
        print(f"#{w['id']:<3} {w['kind']:<5} {(w['name'] or w['target'])[:40]:<40} last={w['last']} {rules}")


def cmd_history(a):
    for r in db().execute("SELECT ts, price, currency FROM prices WHERE watch_id=? ORDER BY ts DESC LIMIT ?",
                          (a.id, a.n)):
        print(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])), r["price"], r["currency"] or "")


def cmd_remove(a):
    con = db()
    con.execute("DELETE FROM watches WHERE id=?", (a.id,))
    con.execute("DELETE FROM prices WHERE watch_id=?", (a.id,))
    con.commit()
    print(f"removed #{a.id}")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):      # e.g. cp950 consoles can't print "≤"; never crash on output
        stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="price alerts to Discord / Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("kind", choices=FETCH)
    p.add_argument("target", nargs="+")
    p.add_argument("--below", type=float)
    p.add_argument("--above", type=float)
    p.add_argument("--drop", type=float, help="percent under the 30-day high")
    p.add_argument("--regex")
    p.add_argument("--name")
    p.add_argument("--cc", default="tw", help="steam region")
    p.set_defaults(fn=cmd_add)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("history")
    p.add_argument("id", type=int)
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("remove")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_remove)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
