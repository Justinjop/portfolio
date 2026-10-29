# Portfolio

Three Python projects about automation that keeps running unattended. Each one runs on a schedule, recovers from failures, and is tested.

| Project | What it does | Highlights |
|---|---|---|
| [signal-desk](signal-desk/) | Crypto-signal research and testnet execution system, run on a 2 GB VM by AI agents within code-enforced limits | least-privilege agents, pre-registered research with a sealed holdout, exchange-side stops and reconciliation, self-repair, 22 unit tests |
| [news-digest-bot](news-digest-bot/) | RSS feeds → Claude-written digest → Discord / Telegram / LINE | multi-channel config, keyword filters, prompt-injection guard, headline fallback, cost table, 9 offline tests |
| [price-watch](price-watch/) | Steam deals, exchange rates and any web price → alert when a target is hit | standard library only, 30-day-high drop rule, no-spam alert state, Windows/Linux schedulers, 8 offline tests |

Nothing here is investment advice. signal-desk trades on an exchange testnet only, and its main result is that the signals it studies lose money after costs.

License: [MIT](LICENSE).

---

三個 Python 自動化專案，重點都是「沒人看著也能穩定運作」：排程執行、出錯自動處理、附自動化測試。

- **signal-desk：** 加密貨幣訊號的研究與測試網交易系統，由權限受限的 AI agent 負責維運和研究。
- **news-digest-bot：** 把新聞來源整理成 AI 摘要，推送到 Discord、Telegram 或 LINE。可以直接接案販售。
- **price-watch：** Steam 特價、匯率、網頁價格到價就通知。
