"""Runs `desk.py hourly` every hour at :01:30 UTC (in a subprocess, so code fixes apply without a restart)."""
import msvcrt
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "daemon.log"


def log(msg: str):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}\n")


def main():
    lock = open(HERE / "daemon.lock", "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("daemon already running")
    log("daemon started")
    while True:
        now = time.time()
        nxt = (now // 3600 + 1) * 3600 + 90
        time.sleep(max(1, nxt - now))
        try:
            r = subprocess.run([sys.executable.replace("pythonw", "python"), str(HERE / "desk.py"), "hourly"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
            log((r.stdout.strip().splitlines() or ["(no output)"])[-1] + (f" | stderr: {r.stderr.strip()[-300:]}"
                                                                         if r.returncode else ""))
        except Exception as e:
            log(f"hourly failed: {e!r}")


if __name__ == "__main__":
    main()
