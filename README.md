# POC

Hourly XAUUSD book. A model reads last week's value area and the nearby support and resistance, then proposes one order. Python checks the idea. [Factor](https://github.com/Fawkes93/factor) places anything that passes on cTrader with the label `POC`.

## How it decides

Each check builds a short picture of gold and asks a model what to do with one book.

1. Read up to 120 days of XAU/USD 15-minute bars from `poc.sqlite`. Each check asks cTrader only for the slices that file does not have, 21 days at a time, newest first. After a restart the saved bars remain, and the request is the gap since the last bar. A rate limit keeps what is already stored. The same bars supply the value area, support, resistance, and trend. If cTrader returns nothing and the file is empty, the map falls back to Reaktor candles.
2. Take the live cTrader bid and ask. Factor keeps that stream open, so a check reads the latest quote instead of opening a new session. The price on the map is the mid. A buy is checked and sent on the ask. A sell is checked and sent on the bid. If the quote is down, use Twelve Data, then the close of the last candle.
3. Build two volume profiles from those bars: the last completed week, and the week still forming. Weeks start on Monday at 00:00 in Europe/Bucharest. Point of control (POC) is the price where the most ticks printed. The value area is the band around it that holds about 70% of that weight. VAH is the top of the band. VAL is the bottom.
4. Send that map, the bid, the ask, the open position, any working pending order, and the recent closed trades to the model. The model answers with one JSON decision: leave the book, place a market order, or place a pending order. It also returns a stop, a target, a confidence score, and up to four prices that should wake an earlier check.

The primary model is `gemini-3.6-flash`, with two tries. A rate limit skips the remaining try. A timeout or HTTP 503 waits 20 seconds and uses the second try. If Flash fails, `gemini-4-hydrogen` takes the same two tries. If that fails too, Grok 4.7 takes the check once. If every model fails, the check stores the error and sends no order.

The book holds one active trade. A new order is blocked while a position is open or a pending order is still working. After that trade is gone, the next check can enter again. On the weekend the check does not ask the model and does not send an order. The open trade is left with its stop.

## How an order is placed

The model never talks to the broker. Python accepts or rejects the idea, then Factor sends the order.

1. If the model asks to cancel the working pending order, that cancel goes to Factor first. A replacement is allowed in the same check only after the cancel is confirmed.
2. If a position is open, the model may leave it, close it, or move the stop to the entry price. A stop is not widened.
3. A new idea is rejected when trading is off, the book is halted, confidence is below 60, the value area is missing, the position list could not be loaded, or another trade is already working.
4. A market order is allowed only while gold is open. A pending order is allowed during the weekday 21:00–22:00 UTC break, and is blocked from Friday 21:00 UTC through Sunday 22:00 UTC. Gold is also closed for that same hour on other weekdays.
5. The stop and target must sit on the correct side of the entry. The stop must be at least $5 away. At the default 0.01 lot, one dollar of gold price is about one dollar of profit or loss, and the stop may not risk more than $25. Reward must be at least 1.2 times the risk.
6. Python chooses the order type. A market order is for a level price is already at. A buy limit rests below the price and a buy stop rests above it. A sell limit rests above the price and a sell stop rests below it. Size is fixed at `VOLUME_LOTS`. The model's own size is ignored.
7. The accepted order is `POST /api/ai/orders` on Factor: account, `XAUUSD`, side, volume, order type, entry, stop, target, and the label `POC`. A market buy is priced on the ask and a market sell on the bid. Factor places it on the cTrader account in `FACTOR_ACCOUNT_ID`.
8. A market fill is stored as an open trade. A limit or stop is stored as the one working pending order. Telegram names the cTrader order id. A later close figure is the quote when the position left Factor's open list, not the broker fill.

`FACTOR_MODE=stub` keeps the same checks and stores the order in memory. Nothing is sent to Factor or cTrader.

## Dependencies

Services:

| Service | Required | What it is used for |
| --- | --- | --- |
| Python 3.11 or newer | Yes | Runs the process |
| Gemini API | Yes | Primary decision, then the Hydrogen fallback |
| Factor | To send orders and read the market | cTrader orders, open positions, 15-minute bars, and the live bid/ask |
| cTrader account | To send orders and read the market | The account id Factor should trade |
| Reaktor candles | Fallback | Used only when the cTrader bars do not load |
| Twelve Data | No | Live price when the cTrader quote does not load. The last candle close is the next fallback |
| xAI | No | Grok 4.7 when both Gemini models fail |
| Telegram | No | Order notes, errors, a 12-hour note, and commands |

Python packages in `requirements.txt`:

| Package | What it is used for |
| --- | --- |
| `httpx` | Calls to Gemini, xAI, Twelve Data, Reaktor, Factor, and Telegram |
| `pydantic` | Parses the model decision |
| `apscheduler` | Hourly check |
| `python-telegram-bot` | Command bot |
| `python-dotenv` | Loads `.env` |
| `pytest` | Tests |

## Install locally

```bash
git clone https://github.com/Fawkes93/poc.git
cd poc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On Windows the virtualenv activates with `.venv\Scripts\activate`.

Fill `.env` before the first run. Startup refuses to start without `GEMINI_API_KEY`, `REAKTOR_SUPABASE_URL`, and `REAKTOR_SUPABASE_KEY`.

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Gemini key |
| `GEMINI_MODEL` | Primary model. Default `gemini-3.6-flash` |
| `GEMINI_FALLBACK_MODEL` | Second Gemini model. Default `gemini-4-hydrogen` |
| `XAI_API_KEY` | Grok key. `GROK_API_KEY` is accepted under the same name |
| `GROK_MODEL` | Default `grok-4.7` |
| `TWELVEDATA_API_KEY` | Fallback live price. Optional when Factor can quote |
| `REAKTOR_SUPABASE_URL` | Reaktor project URL. Required at startup; used when cTrader bars fail |
| `REAKTOR_SUPABASE_KEY` | Key that can read `public.candles` |
| `FACTOR_BASE_URL` | Factor origin, with no path |
| `FACTOR_API_TOKEN` | Bearer token for the Factor AI routes |
| `FACTOR_ACCOUNT_ID` | cTrader account id |
| `FACTOR_MODE` | `http` to send orders, `stub` to keep them in memory |
| `TRADING_ENABLED` | `true` sends orders. The sample file sets `false`. If the variable is omitted, the process default is on |
| `AI_LABEL` | Broker label. Default `POC` |
| `TELEGRAM_BOT_TOKEN` | Bot token. Optional |
| `TELEGRAM_CHAT_ID` | Owner chat, if `/start` has not linked one yet |
| `VOLUME_LOTS` | Fixed size. Default `0.01` |
| `MAX_LOSS_USD` | Most one order may lose at that size. Default `25` |
| `MIN_STOP_USD` | Closest allowed stop. Default `5` |
| `MIN_RR` | Minimum reward divided by risk. Default `1.2` |
| `MIN_CONFIDENCE` | Minimum model score, from 0 to 100. Default `60` |
| `HISTORY_DAYS` | Days of 15-minute bars kept in `poc.sqlite`. Default `120`. cTrader is asked only for the missing slices |
| `LOG_HTTP` | `true` serves the log page. `false` leaves it off |
| `LOG_HTTP_HOST` | Bind address. Default `127.0.0.1` |
| `LOG_HTTP_PORT` | Default `8787` |

```bash
python -m poc --once
python -m poc
pytest -q
```

`python -m poc --once` runs a single check and exits. `python -m poc` stays up: a check at minute 5 of every UTC hour, a price poll every 60 seconds, a log page, and Telegram commands when a bot token is set. When the live price crosses a level the model left, that poll runs one extra check. Another alert check then waits 10 minutes. `.env` and `poc.sqlite` stay on the machine. Neither file is part of the repository.

## Logs

`python -m poc` serves the log at http://127.0.0.1:8787/. The page lists the newest rows and refreshes every 5 seconds. Info, Warning, and Error each keep their own view. Warning is the `warn` level in SQLite. Error also includes rows from the errors table. The page binds to localhost. Set `LOG_HTTP=false` to leave it off.

## Messages

`/start` links the owner chat. `/subscribe` adds another chat. `/unsubscribe` removes it. Subscribed chats receive the same notes and cannot place or flatten trades. `/status`, `/halt`, `/resume`, and `/flat` stay on the owner chat. `/halt` blocks a new entry and leaves an open position in place. `/flat` closes POC positions and cancels the working pending order.

Notes, orders, and errors are stored in `poc.sqlite` beside the process. Errors, and a short note every 12 hours, go to the linked chats.

## Risk

The process can place, cancel, and close broker orders on the configured cTrader account. The checks above reject some ideas. They do not limit losses beyond that per-order cap. Use a demo account until the path is trusted. Run one process only. A second copy can send a second order.
