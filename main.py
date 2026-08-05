import ccxt
import threading
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask
from threading import Thread

# =============================================================
# 1. KEEP-ALIVE WEB SERVER (Fixes Replit Webview Error)
# =============================================================
app = Flask("")


@app.route("/")
def home():
    return "Bot is alive and running!"


def run_web_server():
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()


# =============================================================
# 2. TELEGRAM & TIMEZONE CONFIGURATION
# =============================================================
TELEGRAM_BOT_TOKEN = "8642933768:AAH3afnXGmaAplHDar9u4uwJ5IZz0M7y7fs"
TELEGRAM_CHAT_ID = "7203290966"
IST = ZoneInfo("Asia/Kolkata")

TARGET_IST_TIMES = [
    (1, 30),  # 1:30 AM IST
    (5, 30),  # 5:30 AM IST
    (9, 30),  # 9:30 AM IST
    (13, 30),  # 1:30 PM IST
    (17, 30),  # 5:30 PM IST
    (21, 30),  # 9:30 PM IST
]


def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending Telegram alert: {e}")


# =============================================================
# 3. EXCHANGE & DYNAMIC WATCHLIST SETUP
# =============================================================
exchange = ccxt.binance()
TIMEFRAME = "4h"


def get_all_futures_tokens():
    try:
        markets = exchange.load_markets()
        futures_pairs = [
            symbol
            for symbol, market in markets.items()
            if market.get("linear") and market.get("quote") == "USDT"
        ]
        if not futures_pairs:
            futures_pairs = [s for s in markets.keys() if s.endswith("/USDT")]
        return futures_pairs
    except Exception as e:
        print(f"Error loading markets dynamically: {e}")
        return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]


# =============================================================
# 4. SWEEP PATTERN DETECTION LOGIC
# =============================================================
def check_liquidity_sweep(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=3)
        if not ohlcv or len(ohlcv) < 3:
            return

        prev_high = ohlcv[-2][2]
        prev_low = ohlcv[-2][3]

        curr_high = ohlcv[-1][2]
        curr_low = ohlcv[-1][3]
        curr_close = ohlcv[-1][4]

        # 1. BEARISH SWEEP (SHORT)
        if curr_high > prev_high and curr_close < prev_high:
            msg = (
                f"🚨 *BEARISH SWEEP ALERT (SHORT)* 🚨\n\n"
                f"*Token:* `{symbol}`\n"
                f"*Timeframe:* 4-Hour (IST Schedule)\n"
                f"*Current Close:* `${curr_close}`\n"
                f"*Swept High:* `${curr_high}` (Prev High: `${prev_high}`)\n\n"
                f"💡 *Setup:* Price swept above previous 4H high but closed back below it."
            )
            print(f"[MATCH SHORT] {symbol}")
            send_telegram_alert(msg)

        # 2. BULLISH SWEEP (LONG) WITH PREV HIGH CAP
        elif (
            (curr_low < prev_low)
            and (curr_close > prev_low)
            and (curr_close < prev_high)
        ):
            msg = (
                f"🚨 *BULLISH SWEEP ALERT (LONG)* 🚨\n\n"
                f"*Token:* `{symbol}`\n"
                f"*Timeframe:* 4-Hour (IST Schedule)\n"
                f"*Current Close:* `${curr_close}`\n"
                f"*Swept Low:* `${curr_low}` (Prev Low: `${prev_low}`)\n"
                f"*Prev High Cap:* `${prev_high}`\n\n"
                f"💡 *Setup:* Price swept below previous 4H low, reclaimed above it, and closed below previous high."
            )
            print(f"[MATCH LONG] {symbol}")
            send_telegram_alert(msg)

    except Exception as e:
        print(f"Error checking {symbol}: {e}")


def run_full_scan():
    watchlist = get_all_futures_tokens()
    print(f"\n--- Starting Full Scan across {len(watchlist)} tokens ---")
    for symbol in watchlist:
        check_liquidity_sweep(symbol)
        time.sleep(0.15)
    print("Scan complete across all tokens!")


# =============================================================
# 5. MAIN EXECUTION
# =============================================================
def main():
    print("=== Starting Keep-Alive 24/7 IST 4H Sweep Indicator Bot ===")

    # Start web server on port 8080
    keep_alive()

   def start_scheduler():
  def start_scheduler():
  send_telegram_alert(
      "IST Bot Live! Server started with Webview enabled on 1:30 AM, 5:30 AM,"
      " 9:30 AM, 1:30 PM, 5:30 PM, and 9:30 PM IST schedule."
  )

  last_executed_slot = None

  while True:
    now_utc = datetime.now(timezone.utc)
    current_time_ist = now_utc + timedelta(hours=5, minutes=30)
    current_slot = current_time_ist.strftime("%H:%M")

    target_times = [
        "01:30",
        "05:30",
        "09:30",
        "13:30",
        "17:30",
        "21:30",
    ]

    in_target_window = False
    for target in target_times:
      target_dt = datetime.strptime(target, "%H:%M")
      slot_dt = datetime.strptime(current_slot, "%H:%M")
      time_diff = abs((slot_dt - target_dt).total_seconds())

      if time_diff <= 120:  # Within 2 minutes window
        in_target_window = True
        if last_executed_slot != target:
          run_market_check()
          last_executed_slot = target
        break

    if not in_target_window:
      last_executed_slot = None

    time.sleep(30)


if __name__ == "__main__":
  # 1. Start market scheduler in background thread
  scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
  scheduler_thread.start()

  # 2. Start Flask Web Server for UptimeRobot health checks
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)
