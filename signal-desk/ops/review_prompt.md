Signal Desk hourly review. See PROTOCOL.md and guidelines.md. Use `~/venv/bin/python`.

The working directory is already ~/qt/desk, and the current UTC time is given at the end of this prompt.
- Run each shell command on its own, exactly in the form `~/venv/bin/python desk.py <args>` or `tail -n 5 daemon.log`.
- Do NOT use `cd`, `&&`, `;`, pipes or `date`. Compound commands are rejected by the permission settings.

Steps:
1. Check that the tail of daemon.log shows this hour's run. If it does not, run `systemctl status desk-hourly.service` and report what you find.
2. Run `desk.py pending`.
   - Pre-filter each signal by its features, per guidelines. Skip a signal if any of these hold:
     - price is inside the cloud or at its edge;
     - the cloud is thin;
     - the price has moved more than 1.5 ATR;
     - the price is more than 2 ATR from the kijun;
     - ATR is below 0.5% or above 8%;
     - BTC's 24h move is against the trade by more than 2%;
     - liquidity is low.
   - Read the chart PNG of each remaining candidate and decide.
   - Record EVERY pending signal with `desk.py decide ID take|skip --conf X --note "reason"`.
   - Respect the AI limits: at most 10 open positions, at most 6 on the same side.
3. Run `desk.py review` and read each AI position's chart.
   - Close a position early only when a guideline exit rule applies, using `desk.py close POS --note`.
   - Never widen stops.
4. If the number of closed AI trades has reached a multiple of 50 since the last guideline change, run `desk.py report` and update guidelines.md as a new version per PROTOCOL.md.
5. Run `desk.py manual`. If any manual testnet trades closed since the last review, note them briefly.

Finish with a 2-3 line zh-TW summary only: your decisions, any closes, and each book's PnL from `desk.py report`. Quote each book's `incl. open at mark` line when it is present: the closed-only numbers are biased toward early stop-outs, because winners are still open. Never say a book's CI is below zero from the closed-only line alone.
