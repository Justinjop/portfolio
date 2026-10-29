# Research method

The research question is narrow: can any rule built only from information available at the signal improve on taking every Ichimoku 1h signal, after costs? The default answer is no. The process is built to make a false "yes" expensive.

## Data
- The replay covers about 54k signals:
  - 29 major perps, 2021-01 onward;
  - 119 alts, 2024-01 onward.
- Every trade is replayed with five exit models (`desk`, `stop_time`, `opposite`, `tp3_sl2`, `trail_kijun`) and two entry models (`now`, `pullback`).
- Cost is 0.10% per side. Funding is included in the forward books.
- **Research split:** signal time before 2025-07-01. Agents can only see this split.
- **Sealed holdout:** signal time from 2025-07-01 on.
  - It lives in a `chmod 700` directory that the agent's permission rules deny.
  - Only the orchestrator (`ops/research.py` → `holdout.check`) reads it, and at most once per 7 days.
- **Known leak, recorded in the notebook:** low-ATR findings were checked on 2025–26 data before the split existed. Holdout results for those hypotheses are discounted.

## Per-night protocol (`ops/research_prompt.md`)
1. Read the notebook (long-term memory) and the history of tests run so far.
2. **Pre-register** each hypothesis in `notebook.md` before running anything: rationale, exact spec, and support/reject criteria. Use at most 2 thresholds per hypothesis.
3. Run at most 6 tests a night (`lab.py test`). A duplicate spec returns the stored result and is not counted again.
4. Record every result, including negatives, then the interpretation and the next ideas.

## Statistics (`lab/lab.py: stats`)
- Mean, median, win rate, half-year breakdown and the share of profit from the top 5% of trades.
- 95% CI from a **day-clustered bootstrap**. Signals on the same day are correlated, so resampling individual trades would overstate precision.
- **Bonferroni over all tests ever run.** `adj_lower` is the lower bound after correction, approximated from the bootstrap standard error.
- **★ candidate** needs all of the following. A candidate only earns one look at the holdout. It is never automatically adopted.
  - n ≥ 300 and ≥ 150 distinct days;
  - `adj_lower` > 0;
  - mean above the baseline for the same universe and exit;
  - ≥ 70% of half-years positive.
- **Holdout verdicts:** PASS, 方向一致 (same direction, not significant), or FAIL. Even a PASS only feeds the human checkpoint in `PICKS.md`.

## Forward test (`desk/PROTOCOL.md`)
Five paper books run live on every hourly batch:

| Book | Takes |
|---|---|
| ALL | every signal, kijun exit |
| ALLX | every signal, no kijun exit |
| RAND | a hash-chosen ⅓ of signals (control) |
| AI | Claude's picks from charts |
| RULE1 | the low-ATR rule |

- AI guidelines may change only every 50 closed AI trades.
- The verdict needs ≥ 8 weeks and ≥ 200 trades, judged by a bootstrap CI against ALL and RAND.

## What-if replays for risk parameters (`auto.py whatif`)
- Every pick is replayed under the current policy and one alternative, with entry at the signal price, using 5m bars.
- Trades that haven't exited yet are **marked to market at the last close**. Dropping them would keep only the early stop-outs: the first version did that and showed a 0% win rate.
- Output for each policy:
  - results for the first and second half of the sample separately;
  - the mix of exit reasons;
  - a compounded equity curve at the policy's risk per trade.
- The agent may run at most 8 what-ifs a day, to limit tuning on the same small sample.
