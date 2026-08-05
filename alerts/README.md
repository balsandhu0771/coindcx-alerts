# CoinDCX 4H Fakeout Alert System

A Python script that monitors CoinDCX trading pairs for **price fakeouts** on the previous 4-hour candle's high and low, and delivers real-time alerts via Telegram.

## What It Detects

| Alert | Meaning |
|---|---|
| 🟢 **Break Above 4H High** | Price crossed above the previous 4H candle's high — watch for a reversal |
| 🔴 **Break Below 4H Low** | Price crossed below the previous 4H candle's low — watch for a reversal |
| ⚠️ **Fakeout Confirmed — 4H High** | Price rejected the 4H high and fell back inside — bearish fakeout |
| ⚠️ **Fakeout Confirmed — 4H Low** | Price rejected the 4H low and bounced back inside — bullish fakeout |

## Setup

### 1. Install dependencies

```bash
cd alerts
pip install -r requirements.txt
```

### 2. Configure Telegram (already done via Replit Secrets)

If running locally, create a `.env` file:
```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

How to get these:
- **Bot token**: Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token
- **Chat ID**: Message [@userinfobot](https://t.me/userinfobot) → it replies with your ID

### 3. Edit pairs to watch

Open `config.py` and update `WATCH_PAIRS`:
```python
WATCH_PAIRS = [
    "B-BTC_USDT",
    "B-ETH_USDT",
    "B-SOL_USDT",
    # add more CoinDCX spot pairs here
]
```

### 4. Run

```bash
cd alerts
python main.py
```

## Configuration (`config.py`)

| Setting | Default | Description |
|---|---|---|
| `WATCH_PAIRS` | BTC/ETH/SOL/BNB/XRP | Pairs to monitor |
| `POLL_INTERVAL_SECONDS` | `30` | How often to check prices |
| `CANDLE_REFRESH_EVERY_N_POLLS` | `8` | Refresh 4H candle data every N polls (~4 min) |
| `ALERT_COOLDOWN_SECONDS` | `300` | Min time between repeated alerts (per pair, per type) |

## Pair Format

CoinDCX spot pairs use the format `B-BASE_QUOTE`, for example:
- `B-BTC_USDT` → Bitcoin / USDT
- `B-ETH_USDT` → Ethereum / USDT
- `B-MATIC_USDT` → Polygon / USDT

To find a pair name, check [CoinDCX markets](https://coindcx.com/trade/BTCUSDT).

## How Fakeout Detection Works

1. On startup, the script fetches the **last completed 4H candle** for each pair.
2. Every 30 seconds, it fetches the current last-traded price.
3. It tracks whether price is inside, above, or below the previous 4H range.
4. When price **crosses a level and then reverses**, that is a fakeout — an alert is sent.
5. Candle levels are refreshed automatically every ~4 minutes.

## Running on Replit

The Telegram secrets are already configured. Just open a Shell tab and run:
```bash
cd alerts && python3 main.py
```

To run it persistently as a background service, add a workflow in Replit with the command:
```
cd alerts && python3 main.py
```

## Technical Notes

- CoinDCX's public API exposes `1m`, `15m`, `1h`, `1d` candle intervals — there is no native 4H candle.
  The script fetches the last 16 × 1H candles and aggregates them into UTC-aligned 4H buckets (00–03, 04–07, 08–11, 12–15, 16–19, 20–23), then uses the previous completed bucket as the reference range.
- All API calls use CoinDCX's free public endpoints — no API key is needed for price data.
