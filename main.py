import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import ccxt
from flask import Flask
import requests

# =============================================================
# 1. KEEP-ALIVE WEB SERVER & MANUAL TRIGGER ROUTE
# =============================================================
app = Flask(__name__)


@app.route("/")
def home():
  return "Bot is alive and running!", 200


@app.route("/trigger-scan")
def trigger_manual_scan():
  # Starts full scan in background thread
  threading.Thread(target=run_full_scan, daemon=True).start()
  return (
      "Manual 4H market scan (with 7-Day $5M Volume Filter) started! Check"
      " Telegram in 2 minutes.",
      200,
  )


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
# 3. EXCHANGE & WATCHLIST SETUP (7-Day $5M Daily Volume Filter)
# =============================================================
exchange = ccxt.binance({
    "enableRateLimit": True,
    "timeout": 30000,
    "options": {"defaultType": "future"},  # Loads USDT-M Futures markets
})
TIMEFRAME = "4h"
MIN_7D_AVG_VOLUME = 5_000_000  # $5 Million USD Daily Volume Threshold


def get_all_futures_tokens():
  try:
    markets = exchange.load_markets()

    # Step 1: Filter active USDT linear futures symbols
    all_pairs = [
        symbol
        for symbol, market in markets.items()
        if market.get("swap")
        and market.get("linear")
        and market.get("quote") == "USDT"
        and market.get("active", True)
    ]

    print(f"Loading 7-Day Volume for {len(all_pairs)} futures markets...")

    filtered_pairs = []
    for symbol in all_pairs:
      try:
        # Fetch last 8 daily candles (7 completed + 1 current active day)
        ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=8)
        if len(ohlcv_1d) >= 8:
          # Sum volume across 7 closed daily candles (index 1 to 7)
          total_7d_vol = sum(candle[5] for candle in ohlcv_1d[1:8])
          avg_daily_vol = total_7d_vol / 7.0

          if avg_daily_vol >= MIN_7D_AVG_VOLUME:
            filtered_pairs.append(symbol)

        time.sleep(0.05)  # Safe rate-limit delay
      except Exception as inner_e:
        print(f"Volume check skipped for {symbol}: {inner_e}")
        # Default keep liquid majors if fetch fails on specific symbol
        if symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]:
          filtered_pairs.append(symbol)

    print(
        f"Filtered Watchlist: {len(filtered_pairs)} tokens meet the > $5M 7-Day"
        " Avg Daily Volume rule."
    )

    if filtered_pairs:
      return filtered_pairs

    return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]

  except Exception as e:
    print(f"Error loading market list: {e}")
    return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]


# =============================================================
# 4. SWEEP PATTERN DETECTION LOGIC
# =============================================================
def check_liquidity_sweep(symbol):
  try:
    # Fetch 4 candles to evaluate the newly closed candle safely
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=4)
    if not ohlcv or len(ohlcv) < 4:
      return False

    # Index -2 is the newly closed 4H candle; Index -3 is the previous candle
    prev_high = ohlcv[-3][2]
    prev_low = ohlcv[-3][3]

    closed_high = ohlcv[-2][2]
    closed_low = ohlcv[-2][3]
    closed_close = ohlcv[-2][4]

    # 1. BEARISH SWEEP (SHORT)
    if closed_high > prev_high and closed_close < prev_high:
      msg = (
          f"🚨 *BEARISH SWEEP ALERT (SHORT)* 🚨\n\n"
          f"*Token:* `{symbol}`\n"
          f"*Timeframe:* 4-Hour (IST Schedule)\n"
          f"*Closed Price:* `${closed_close}`\n"
          f"*Swept High:* `${closed_high}` (Prev High: `${prev_high}`)\n\n"
          f"💡 *Setup:* Price swept above previous 4H high and closed back"
          f" below it."
      )
      print(f"[MATCH SHORT] {symbol}")
      send_telegram_alert(msg)
      return True

    # 2. BULLISH SWEEP (LONG) WITH PREV HIGH CAP
    elif (
        (closed_low < prev_low)
        and (closed_close > prev_low)
        and (closed_close < prev_high)
    ):
      msg = (
          f"🚨 *BULLISH SWEEP ALERT (LONG)* 🚨\n\n"
          f"*Token:* `{symbol}`\n"
          f"*Timeframe:* 4-Hour (IST Schedule)\n"
          f"*Closed Price:* `${closed_close}`\n"
          f"*Swept Low:* `${closed_low}` (Prev Low: `${prev_low}`)\n"
          f"*Prev High Cap:* `${prev_high}`\n\n"
          f"💡 *Setup:* Price swept below previous 4H low, reclaimed above it,"
          f" and closed below previous high."
      )
      print(f"[MATCH LONG] {symbol}")
      send_telegram_alert(msg)
      return True

    return False

  except Exception as e:
    print(f"Error checking {symbol}: {e}")
    return False


def run_full_scan():
  watchlist = get_all_futures_tokens()
  print(f"\n--- Starting Full Scan across {len(watchlist)} tokens ---")

  alerts_triggered = 0
  for symbol in watchlist:
    try:
      matched = check_liquidity_sweep(symbol)
      if matched:
        alerts_triggered += 1
      time.sleep(0.12)  # Safe rate limit delay
    except Exception as e:
      print(f"Error in scanning loop for {symbol}: {e}")
      time.sleep(0.5)

  summary_msg = (
      f"🔍 *4H Scheduled Scan Complete*\n"
      f"• *Tokens Filtered (> $5M 7d Vol):* `{len(watchlist)}`\n"
      f"• *Sweep Setups Found:* `{alerts_triggered}`"
  )
  send_telegram_alert(summary_msg)
  print(f"Scan complete across {len(watchlist)} quality tokens!")


# =============================================================
# 5. SCHEDULER & MAIN EXECUTION
# =============================================================
def start_scheduler():
  print("=== Starting 24/7 Market Monitor ===")

  send_telegram_alert(
      "IST Bot Live! Server started with Webview enabled on 1:30 AM, 5:30 AM,"
      " 9:30 AM, 1:30 PM, 5:30 PM, and 9:30 PM IST schedule."
  )

  last_executed_slot = None

  while True:
    now_ist = datetime.now(IST)

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
      target_hour, target_minute = map(int, target.split(":"))
      target_dt = now_ist.replace(
          hour=target_hour, minute=target_minute, second=0, microsecond=0
      )

      # Seconds difference between current time and scheduled slot
      time_diff = (now_ist - target_dt).total_seconds()

      # Triggers between 0 and 180 seconds AFTER the slot time
      if 0 <= time_diff <= 180:
        in_target_window = True
        if last_executed_slot != target:
          run_full_scan()
          last_executed_slot = target
        break

    if not in_target_window:
      last_executed_slot = None

    time.sleep(15)


if __name__ == "__main__":
  # 1. Start market scheduler in background thread
  scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
  scheduler_thread.start()

  # 2. Start Flask Web Server for UptimeRobot health checks
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)
