# news-digest-bot

Turn any set of RSS/Atom feeds into a short AI-written digest, posted to Discord, Telegram, or LINE on a schedule. Each channel has its own sources, keyword filters, audience and writing instructions, and remembers what it already posted.

[中文說明在下方](#中文說明)

## Features
- **Many channels from one config:** for example a tech digest for one Discord server and a game-news digest for another.
- **Filters:** `include` keywords (only matching items) and `exclude` keywords (drop ads and deals). Duplicate stories are merged by URL and by title.
- **Summaries by Claude**, through the official Anthropic SDK. The prompt tells the model to treat feed text as untrusted, so injected instructions inside articles are not followed.
- **Never silent:** without an API key, or when a call fails or is declined, it posts a plain headline list instead.
- **Platform-safe:** long digests are split under each platform's message limit. LINE uses the Messaging API, since LINE Notify was shut down in 2025.
- **Tested:** `python -m unittest test_digest` runs offline. It covers feed parsing, de-duplication, the time window, keyword filters, message splitting, the no-key path and the failure fallback.

## Setup
1. Python 3.10+, then `pip install -r requirements.txt`. The `anthropic` package is only needed for AI summaries.
2. Copy `config.example.json` to `config.json`. Edit the feeds and instructions, and fill in a destination:
   - Discord: a channel webhook URL.
   - Telegram: a bot token and chat id.
   - LINE: a Messaging API channel access token and a user or group id.
3. Set `ANTHROPIC_API_KEY` in the environment for AI summaries. Without it you get headline lists.
4. Try it: `python digest.py --dry-run` prints the digest without posting or marking anything as seen.
5. Schedule `python digest.py` hourly or daily: cron, a systemd timer, or Windows Task Scheduler (`.\schedule_windows.ps1` registers an hourly Windows task; `-Minutes 1440` for daily).

## Cost
The model is set in `config.json` (`"model"`, default `claude-opus-5-5`), and so is the effort (`"effort"`, default `medium`).

A digest of about 40 items sends about 5–6K input tokens and gets back about 2K tokens of output, plus some thinking tokens. Rough cost per run at list prices:

| Model | ≈ per run | hourly (≈720 runs/mo) | daily (30 runs/mo) |
|---|---|---|---|
| `claude-opus-5-5` | US$0.06–0.12 | US$45–85 | US$2–4 |
| `claude-sonnet-5-5` | US$0.03–0.06 | US$22–43 | US$1–2 |
| `claude-haiku-5-5` | ≈ US$0.002 | ≈ US$1.5 | < US$0.1 |

These are estimates, so check `usage` in the Anthropic Console after a few runs. For most communities, one or two digests a day is enough.

`refusal_fallback` (on by default) uses the API's server-side fallback, so a batch the main model declines is retried on another model instead of failing.

---

## 中文說明

把你指定的新聞來源（RSS／Atom）整理成 AI 寫的重點摘要，定時推送到 Discord、Telegram 或 LINE。

- **一份設定可以有很多頻道：** 每個頻道各自設定來源、關鍵字篩選、讀者對象和寫作要求，例如一個科技新聞頻道和一個遊戲新聞頻道。
- **自動去重：** 同一則新聞被多家報導會合併；已經發過的不會再發。
- **不會斷線：** 沒有 API 金鑰、或 AI 呼叫失敗時，會改發標題清單，頻道不會沒有內容。
- **防提示注入：** 新聞內文被當成不可信的資料，只摘要，不執行其中的指示。
- **成本：** 見上表。每小時摘要用 Opus 約每月 US$45–85；每天 1 次只要 US$2–4，也可以改用更便宜的模型。

**接案時的報價參考：** 建置費 NT$3,000 起，代管每月 NT$300 起，另外向客戶說明 API 費用由客戶支付，或包含在代管費裡（依上表估算）。
