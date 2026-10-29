"""Add market-regime and funding features to the lab's research set AND the sealed set (user decision 2026-10-29).

Run by the orchestrator only (it touches sealed.pkl). It only adds feature columns computed from data known at
signal time; it never reads net_bp. Re-running replaces the columns, so it is idempotent.

Timing: a row's t is the open time of the signal bar; the signal uses that bar's close, so it is known at
te = t + 1h (entry is the next bar's open). Every feature below uses only data available at te.
  btc_ret7d        BTC close at te / BTC close 168h earlier - 1
  btc_ret30d       same over 720h
  btc_ret30d_side  side * btc_ret30d  (> 0: the signal points the same way as BTC's 30-day trend)
  btc_cloud        BTC daily Ichimoku (9/26/52) on the last CLOSED UTC day: +1 above cloud, -1 below, 0 inside
  funding_8h       last funding rate settled at or before te, scaled to an 8h interval
  funding_side     side * funding_8h   (> 0: the signal's side pays funding, i.e. it joins the crowded side)
Data: data.binance.vision monthly zips (SHA256-verified), cached in ~/qt/desk/hist/regime_raw. Alt funding zips
already downloaded for the alt replication are reused from ~/qt/desk/hist/alts/raw/fr.
"""
import hashlib
import io
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HOME = Path.home() / "qt"
FILES = [HOME / "lab" / "data" / "research.pkl", HOME / "lab_sealed" / "sealed.pkl"]
RAW = HOME / "desk" / "hist" / "regime_raw"
ALT_FR = HOME / "desk" / "hist" / "alts" / "raw" / "fr"
KL = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/1h/{s}-1h-{m}.zip"
FR = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip"
NEW = ["btc_ret7d", "btc_ret30d", "btc_ret30d_side", "btc_cloud", "funding_8h", "funding_side"]


def months(a, b):
    return [p.strftime("%Y-%m") for p in pd.period_range(a, b, freq="M")]


def fetch(url):
    for _ in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
    raise RuntimeError(url)


def get(job):
    kind, s, m = job
    dst = RAW / kind / s / f"{m}.zip"
    if dst.exists():
        return "cached"
    url = (KL if kind == "kl" else FR).format(s=s, m=m)
    b = fetch(url)
    if b is None:
        return "missing"
    chk = fetch(url + ".CHECKSUM")
    if chk is None or chk.split()[0].decode() != hashlib.sha256(b).hexdigest():
        raise RuntimeError(f"checksum {url}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.with_suffix(".tmp").write_bytes(b)
    dst.with_suffix(".tmp").replace(dst)
    return "ok"


def read_csvs(folder: Path, names):
    out = []
    for z in sorted(folder.glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            raw = zf.read(zf.namelist()[0])
        header = not raw[:1].isdigit()
        f = pd.read_csv(io.BytesIO(raw), header=0 if header else None)
        f = f.iloc[:, :len(names)]
        f.columns = names[:f.shape[1]]
        out.append(f)
    return out


def btc_frame():
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    k = pd.concat(read_csvs(RAW / "kl" / "BTCUSDT", cols)).drop_duplicates("open_time").sort_values("open_time")
    k.index = pd.to_datetime(k.open_time, unit="ms")      # naive UTC throughout this script
    return k[["open", "high", "low", "close"]].astype(float)


def daily_cloud(h):
    d = h.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    mid = lambda n: (d.high.rolling(n).max() + d.low.rolling(n).min()) / 2
    a, b = ((mid(9) + mid(26)) / 2).shift(26), mid(52).shift(26)
    top, bot = np.maximum(a, b), np.minimum(a, b)
    state = np.where(d.close > top, 1.0, np.where(d.close < bot, -1.0, 0.0))
    state[top.isna().to_numpy()] = np.nan
    # day D's state is known once D has closed, i.e. from D + 1 day
    return pd.DataFrame({"avail": d.index + pd.Timedelta(days=1), "btc_cloud": state}).dropna()


def funding_series(sym):
    names = ["calc_time", "funding_interval_hours", "last_funding_rate"]
    parts = read_csvs(RAW / "fr" / sym, names) + read_csvs(ALT_FR / sym, names)
    if not parts:
        return None
    norm = []
    for p in parts:
        if "last_funding_rate" in p:
            rate, hrs = p.last_funding_rate.astype(float), p.funding_interval_hours.astype(float)
        else:                                             # two-column files: (calc_time, rate), 8h interval
            rate, hrs = p.funding_interval_hours.astype(float), 8.0
        norm.append(pd.DataFrame({"ft": pd.to_datetime(p.calc_time.astype("int64"), unit="ms").dt.round("min"),
                                  "funding_8h": rate * 8.0 / hrs}))
    return pd.concat(norm).drop_duplicates("ft").sort_values("ft")


def add(df, btc, cloud, fund):
    df = df.drop(columns=[c for c in NEW if c in df], errors="ignore")
    te = pd.DatetimeIndex(df.t.dt.tz_convert("UTC").dt.tz_localize(None) + pd.Timedelta(hours=1))
    c = btc.close.copy()
    c.index = c.index + pd.Timedelta(hours=1)            # close of the bar opening at x is known at x + 1h
    at = lambda ts: c.reindex(ts).to_numpy()
    now = at(te)
    df["btc_ret7d"] = now / at(te - pd.Timedelta(hours=168)) - 1
    df["btc_ret30d"] = now / at(te - pd.Timedelta(hours=720)) - 1
    df["btc_ret30d_side"] = df.side * df.btc_ret30d
    key = pd.DataFrame({"te": te, "i": np.arange(len(df))}).sort_values("te")
    m = pd.merge_asof(key, cloud, left_on="te", right_on="avail")
    df["btc_cloud"] = m.sort_values("i").btc_cloud.to_numpy()
    fr = np.full(len(df), np.nan)
    for sym, idx in df.groupby("sym", observed=True).indices.items():
        f = fund.get(sym)
        if f is None or f.empty:
            continue
        k = pd.DataFrame({"te": te[idx], "i": idx}).sort_values("te")
        mm = pd.merge_asof(k, f, left_on="te", right_on="ft", tolerance=pd.Timedelta(hours=9))
        fr[mm.i.to_numpy()] = mm.funding_8h.to_numpy()
    df["funding_8h"] = fr
    df["funding_side"] = df.side * df.funding_8h
    return df


def main():
    frames = [pd.read_pickle(p) for p in FILES]
    syms = sorted(set().union(*(set(f.sym.astype(str)) for f in frames)))
    majors = sorted(set().union(*(set(f[f.universe == "majors"].sym.astype(str)) for f in frames)))
    jobs = [("kl", "BTCUSDT", m) for m in months("2020-06", "2026-09")]
    jobs += [("fr", s, m) for s in majors for m in months("2020-12", "2026-09")]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(get, jobs))
    print("download:", {k: res.count(k) for k in set(res)}, flush=True)
    btc = btc_frame()
    cloud = daily_cloud(btc)
    fund = {s: funding_series(s) for s in syms}
    print(f"btc 1h {btc.index.min():%F}..{btc.index.max():%F}; funding for {sum(v is not None for v in fund.values())}/{len(syms)} symbols")
    for p, f in zip(FILES, frames):
        bak = p.with_name(p.name + ".bak-prefeatures")
        if not bak.exists():
            shutil.copy2(p, bak)
        out = add(f, btc, cloud, fund)
        assert len(out) == len(f) and (out.net_bp.to_numpy() == f.net_bp.to_numpy()).all()
        out.to_pickle(p.with_suffix(".tmp"))
        p.with_suffix(".tmp").replace(p)
        cov = out.groupby("universe", observed=True)[NEW].apply(lambda g: g.notna().mean().round(3))
        print(f"\n{p.name}: {len(out)} rows; non-missing share by universe:\n{cov.to_string()}")
    sys.exit(0)


if __name__ == "__main__":
    main()
