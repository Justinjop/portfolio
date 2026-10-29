"""One-time split of the replay tables into the lab's research set and the sealed holdout.

research  (t <  2025-07-01) -> ~/qt/lab/data/research.pkl     readable by the research agent
sealed    (t >= 2025-07-01) -> ~/qt/lab_sealed/sealed.pkl      only holdout.py (run by the orchestrator) reads it
The source pickles are moved into ~/qt/lab_sealed/orig so nothing in ~/qt/lab contains sealed rows.
"""
import shutil
from pathlib import Path

import pandas as pd

LAB = Path.home() / "qt" / "lab"
SEALED = Path.home() / "qt" / "lab_sealed"
CUT = pd.Timestamp("2025-07-01", tz="UTC")


def main():
    (SEALED / "orig").mkdir(parents=True, exist_ok=True)
    parts = []
    for name, uni in (("replay.pkl", "majors"), ("replay_alts.pkl", "alts")):
        src = LAB / "data" / name
        d = pd.read_pickle(src).assign(universe=uni)
        parts.append(d)
        shutil.move(str(src), SEALED / "orig" / name)
    df = pd.concat(parts, ignore_index=True)
    df = df.drop_duplicates(["sym", "t", "entry", "exit"], keep="first")      # a symbol in both lists counts once
    df["hour"] = df.t.dt.hour
    df["weekday"] = df.t.dt.weekday
    for c in ("universe", "entry", "exit", "reason", "sym"):
        df[c] = df[c].astype("category")
    res, sea = df[df.t < CUT], df[df.t >= CUT]
    res.reset_index(drop=True).to_pickle(LAB / "data" / "research.pkl")
    sea.reset_index(drop=True).to_pickle(SEALED / "sealed.pkl")
    sig = lambda d: len(d[(d.entry == "now") & (d.exit == "desk")])
    print(f"research: {len(res)} rows, {sig(res)} signals, {res.t.min():%F}..{res.t.max():%F}")
    print(f"sealed:   {len(sea)} rows, {sig(sea)} signals, {sea.t.min():%F}..{sea.t.max():%F}")


if __name__ == "__main__":
    main()
