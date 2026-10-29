# Safety model

The system runs unattended and gives AI agents real responsibilities. This page is the set of rules that does not depend on the agents behaving well.

## 1. Money
- **No production keys exist on the host.** The executor's base URL is a constant (`https://demo-fapi.binance.com`), and an `assert` in `Api.__init__` stops the program if it is anything else.
- **Every position has an exchange-side stop.** The stop is a conditional algo order, `STOP_MARKET`, triggered by mark price.
  - It is placed right after the fill, with one retry.
  - If it still fails, the position is closed with a reduce-only market order.
  - Every 5 minutes the executor checks that each position's stop is still active, and re-places any that are missing.
- **Order idempotency.**
  - The trade row (`status='opening'`) and its deterministic client id `dsk-<id>-e` are written before the order is sent.
  - Order POSTs are never blindly retried. When the outcome is unknown (for example a timeout), the next run queries the order by its client id.
- **Isolation from other actors.**
  - Symbols that another bot uses are never traded (BTC/ETH, which belong to bfbot).
  - Neither is any symbol where someone else holds a position or has open orders.
  - The manual-trade sync excludes `dsk-` orders and every fill inside an auto trade's time window.
- **Hard caps.**
  - The policy file is clamped to `BOUNDS`.
  - Gross exposure is capped at 3× equity, and a single position at 40% of equity.
  - A daily-loss breaker halts new entries for 24h when equity falls a set percentage below its 24h peak.

## 2. Agents
Every agent is a headless `claude -p --permission-mode dontAsk` run with an explicit allowlist and denylist:

| Agent | May | May not |
|---|---|---|
| nightly research | read docs, edit `notebook.md`, run `lab.py describe/history/test` | read the sealed holdout, write files, use the web, read secrets |
| daily risk tuning | read the journal, policy and code; edit `journal.md`; run `auto.py status/report/whatif/policy/set` | edit the policy file or the code, run/flatten/pause/resume, read keys |
| repair diagnosis | read the project, journald, systemctl status | edit anything, sudo, network |
| hourly review | read charts, run desk commands | (see `ops/review_prompt.md`) |

- Absolute paths in permission rules need `//` (`Read(//home/...)`). A single leading `/` is resolved relative to the project, and that mistake silently denied the first research run.
- Parameter changes go through `auto.py set`, which enforces:
  - bounds;
  - steps of 0.67–1.5× per change;
  - cadence: de-risking at most 1×/day; any other change at most 1×/day, and only after ≥7 days and ≥30 closed trades since the last change;
  - a written reason of at least 20 characters.

  The user can bypass all of this with `AUTO_OVERRIDE=1`. The agent cannot, because environment-variable prefixes don't match its allowlisted commands.
- Reports mix the agent's text with **facts generated from the database**: test results, policy changes, equity.
- Webhook URLs and API tokens are masked by `redact()` before anything is posted.

## 3. Operations
- The watchdog checks:
  - timer state and staleness;
  - failed services;
  - error rows in the database;
  - suspicious log lines (Claude failures, tracebacks, 401s, permission denials, `AUTO-ERROR`);
  - disk, memory and swap;
  - backup age.
- Alerts repeat every 6h while a problem persists, and a recovery notice is sent when it clears.
- The deterministic repair playbook runs through `sudo -n`, on a whitelist of units, once per key per 6h and at most 6 times a day. Code changes are always left to a human.
- Daily backups use sqlite's online backup API plus `integrity_check`, are kept for 14 days, and a copy is posted to Discord. Secret files are excluded by name.
