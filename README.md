# POC

Hourly XAUUSD book. Gemini reads the weekly value area and nearby support and resistance, then proposes one order. Python checks the idea. [Factor](https://github.com/Fawkes93/factor) places anything that passes on cTrader with the label `POC`.

The primary model is `gemini-3.6-flash`. Each Gemini model gets two tries. A rate limit skips the remaining tries of that model. A timeout or HTTP 503 waits 20 seconds and uses the second try. If Flash fails, `gemini-4-hydrogen` takes the check. If that fails too, Grok 4.7 takes it once.

## Map

The context is last week's point of control, value-area high, and value-area low, plus the week still forming. Those prices come from cTrader 15-minute tick volume through Factor. If that request fails or returns no bars, the profile falls back to Reaktor candles. Support, resistance, and the weekly and daily trend always use the Reaktor candles.

A market order is for a level price is already at. A pending limit or stop is for a level price still has to reach. The book holds one active trade. A new order is blocked while a position or a pending order is working. After it closes, the next check can enter again. The model can leave prices that wake a check before the next hour. Loss on 0.01 lot is capped at $25.

## Schedule

Checks run at minute 5 of every UTC hour. Gold is closed from Friday 21:00 UTC through Sunday 22:00 UTC. On other weekdays the market is closed from 21:00 to 22:00 UTC. A pending order is still allowed during that weekday break. A market order is not. The weekend blocks both.

## State and messages

Notes, orders, and errors are stored in `poc.sqlite` beside the process. That file stays on the machine that runs the process.

Telegram messages name the cTrader order id. A close figure is the quote when the position left the open list, not the broker fill. Errors, and a short note every 12 hours, go to the same chats.

`/start` links the owner chat. `TELEGRAM_CHAT_ID` does the same when it is set before the first `/start`. `/subscribe` adds another chat. `/unsubscribe` removes it. Subscribed chats receive notes only. `/status`, `/halt`, `/resume`, and `/flat` stay on the owner chat.

## Requirements

- Python 3.11 or newer
- A Factor deployment that accepts the `POC` label and serves `GET /api/ai/trendbars`
- Reaktor `public.candles` for XAU/USD, read only
- A Gemini API key
- Optional: an xAI key for the Grok fallback, a Twelve Data key for the live price, a Telegram bot token

The process reads Reaktor candles and the Twelve Data last price. It does not write those tables. Price falls back to the last closed candle when Twelve Data has no quote.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`. Required at startup: `GEMINI_API_KEY`, `REAKTOR_SUPABASE_URL`, and `REAKTOR_SUPABASE_KEY`. Order calls also need `FACTOR_BASE_URL`, `FACTOR_API_TOKEN`, and `FACTOR_ACCOUNT_ID`.

The sample file sets `TRADING_ENABLED=false`. Set it to `true` to send orders. If that variable is omitted, the process default is on. `FACTOR_MODE` defaults to `http`.

```bash
python -m poc --once
python -m poc
pytest -q
```

`python -m poc` runs the hourly schedule, the price-alert poll, and Telegram commands when `TELEGRAM_BOT_TOKEN` is set.

## Risk

The process can place, cancel, and close broker orders on the configured cTrader account. Size, loss, and session checks reject some ideas. They do not cap account loss beyond the per-order and daily limits in `.env`. Run against a demo account until the path is trusted. One process only. A second copy can send a second order.
