import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import ccxt
from flask import Flask
import requests

# =============================================================
# 1. KEEP-ALIVE WEB SERVER (For UptimeRobot & Render)
# =============================================================
app = Flask(__name__)


@app.route("/")
def home():
  return "Bot is alive and running!", 200


# =============================================================
# 2. TELEGRAM & TIMEZONE CONFIGURATION
# =============================================================
TELEGRAM_BOT_TOKEN = "8642933768:AAH3afnXGmaAplHDar9u4uwJ5IZz0M7y7fs"
TELEGRAM_CHAT_ID = "7203290966"
IST = ZoneInfo("Asia/Kolkata")


def send_telegram_alert(message):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print(f"Error sending Telegram alert: {e}")


# =============================================================
# 3. EXCHANGE & DYNAMIC WATCHLIST SETUP (CoinDCX)
# =============================================================
exchange = ccxt.coindcx({"enableRateLimit": True})
TIMEFRAME = "4h"


def get_all_futures_tokens():
  try:
    markets = exchange.load_markets()
    # Filter for USDT trading pairs on CoinDCX
    pairs = [s for s in markets.keys() if s.endswith("/USDT")]
    if not pairs:
      pairs = list(markets.keys())
    return pairs
  except Exception as e:
    print(f"Error loading CoinDCX markets: {e}")
    return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]


# =============================================================
# 4. SWEEP PATTERN DETECTION LOGIC
# =============================================================
def check_liquidity_sweep(symbol):
  try:
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=3)
    if not ohlcv or len(ohlcv) < 3:
      return False

    prev_high = ohlcv[-2][2]
    prev_low = ohlcv[-2][3]

    curr_high = ohlcv[-1][2]
    curr_low = ohlcv[-1][3]
    curr_close = ohlcv[-1][4]

    # 1. BEARISH SWEEP (SHORT)
    if curr_high > prev_high and curr_close < prev_high:
      msg = (
          f"🚨 *BEARISH SWEEP ALERT (SHORT)* 🚨\n\n"
          f"*Exchange:* `CoinDCX`\n"
          f"*Token:* `{symbol}`\n"
          f"*Timeframe:* 4-Hour (IST Schedule)\n"
          f"*Current Close:* `${curr_close}`\n"
          f"*Swept High:* `${curr_high}` (Prev High: `${prev_high}`)\n\n"
          f"💡 *Setup:* Price swept above previous 4H high but closed back"
          f" below it."
      )
      print(f"[MATCH SHORT] {symbol}")
      send_telegram_alert(msg)
      return True

    # 2. BULLISH SWEEP (LONG) WITH PREV HIGH CAP
    elif (
        (curr_low < prev_low)
        and (curr_close > prev_low)
        and (curr_close < prev_high)
    ):
      msg = (
          f"🚨 *BULLISH SWEEP ALERT (LONG)* 🚨\n\n"
          f"*Exchange:* `CoinDCX`\n"
          f"*Token:* `{symbol}`\n"
          f"*Timeframe:* 4-Hour (IST Schedule)\n"
          f"*Current Close:* `${curr_close}`\n"
          f"*Swept Low:* `${curr_low}` (Prev Low: `${prev_low}`)\n"
          f"*Prev High Cap:* `${prev_high}`\n\n"
          f"💡 *Setup:* Price swept below previous 4H low, reclaimed above it,"
          f" and closed below previous high."
      )
      print(f"[MATCH LONG] {symbol}")
      send_telegram_alert(msg)
      return True

    return False

  except Exception as e:
    print(f"Error checking {symbol} on CoinDCX: {e}")
    return False


def run_full_scan():
  watchlist = get_all_futures_tokens()
  print(f"\n--- Starting CoinDCX Scan across {len(watchlist)} tokens ---")

  alerts_triggered = 0
  for symbol in watchlist:
    matched = check_liquidity_sweep(symbol)
    if matched:
      alerts_triggered += 1
    time.sleep(0.25)  # Pause to respect CoinDCX rate limits

  # Summary message sent to Telegram
  summary_msg = (
      f"🔍 *CoinDCX Scheduled Scan Complete*\n"
      f"• *Tokens Checked:* `{len(watchlist)}`\n"
      f"• *Sweep Alerts Found:* `{alerts_triggered}`"
  )
  send_telegram_alert(summary_msg)
  print("CoinDCX scan complete!")


# =============================================================
# 5. SCHEDULER & MAIN EXECUTION
# =============================================================
def start_scheduler():
  print("=== Starting 24/7 CoinDCX Market Monitor ===")

  send_telegram_alert(
      "IST Bot Live! Server started with Webview enabled on 1:30 AM, 5:30 AM,"
      " 9:30 AM, 1:30 PM, 5:30 PM, and 9:30 PM IST schedule."
  )

  last_executed_slot = None

  while True:
    now_ist = datetime.now(IST)
    current_slot = now_ist.strftime("%H:%M")

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

      if time_diff <= 120:  # Within 2-minute window
        in_target_window = True
        if last_executed_slot != target:
          run_full_scan()
          last_executed_slot = target
        break

    if not in_target_window:
      last_executed_slot = None

    time.sleep(20)


if __name__ == "__main__":
  # 1. Start market scheduler in background thread
  scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
  scheduler_thread.start()

  # 2. Start Flask Web Server for UptimeRobot health checks
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)
