# price-watch

Get a Discord or Telegram message when a price hits your target: Steam deals, exchange rates, or the price on any web page. It's one file, uses only the Python standard library, and keeps price history in sqlite.

[中文說明在下方](#中文說明)

```
$ python watch.py add steam 1091500 --below 500 --drop 40     # Cyberpunk 2077: ≤ NT$500 or 40% under its 30-day high
$ python watch.py add fx USD TWD --below 31.5                   # tell me when USD/TWD drops to 31.5
$ python watch.py add page https://shop.example.com/item \
      --regex 'class="price">([^<]+)<' --below 999 --name "Keyboard"
$ python watch.py check
#1 Cyberpunk 2077: 1599.0
#2 USD/TWD: 32.4159
#3 Keyboard: 949.0 ALERT ≤ 999
```

## Features
- **Three sources:**
  - Steam store (any region, `--cc`);
  - exchange rates (open.er-api.com, no key needed);
  - any page, through a regex with one capture group. Prices like `NT$1,299`, `$12.99` and `1.299,50 €` are all parsed.
- **Rules:** `--below`, `--above`, and `--drop P` (P% under the 30-day high, which catches "real" sales, not inflated list prices).
- **No spam:** a watch alerts once when a rule starts hitting, again only on a new low or when another rule starts hitting, and re-arms after the rule clears.
- **Robust:** one broken source never stops the other watches, and requests are spaced out.
- **Tested:** `python -m unittest test_watch` runs offline (parsers, rules, the alert/re-arm sequence).

## Setup
1. Python 3.10+. Nothing to install.
2. Copy `config.example.json` to `config.json` and add a Discord webhook, Telegram bot settings, or both.
3. Schedule `python watch.py check` every 30–60 minutes:
   - Windows: `.\schedule_windows.ps1` registers a Task Scheduler task (`-Minutes 60` to change the interval, `-Remove` to delete it);
   - Linux: copy `systemd/price-watch.*` to `~/.config/systemd/user/` and run `systemctl --user enable --now price-watch.timer`, or use cron: `*/30 * * * * cd /path/to/price-watch && python3 watch.py check`.

Before you watch a page, check the site's terms of use and `robots.txt`, and keep the interval reasonable. For sites that offer an official API, use the API.

## Commands
| Command | |
|---|---|
| `add steam APPID [--cc tw]` | Steam app (the number in the store URL) |
| `add fx BASE QUOTE` | exchange rate, e.g. `fx JPY TWD` |
| `add page URL --regex RE [--name N]` | any page; RE's first group is the price |
| `check` | fetch everything once and send alerts |
| `list` / `history ID [-n 20]` / `remove ID` | manage watches |

---

## 中文說明

價格到你設定的條件時，自動傳 Discord 或 Telegram 通知給你。

- **支援的來源：** Steam 特價（可指定地區）、匯率到價、任意網頁價格（用 regex 抓）。
- **通知規則：**
  - 低於某價格；
  - 高於某價格（適合匯率）；
  - 比 30 天高點低 P%：可以抓到真正的降價，而不是先漲價再打折。
- **不會洗版：** 條件剛成立時通知一次；之後只有出現更低價，或有新的條件成立，才會再通知。條件解除後重新計算。
- **使用方式：** 只要 Python，不用安裝任何套件。設定好 `config.json` 後，用排程每 30–60 分鐘執行一次 `python watch.py check`。
- **注意：** 抓網頁前先確認對方網站的使用條款，頻率不要太高；有官方 API 的網站優先用 API。
