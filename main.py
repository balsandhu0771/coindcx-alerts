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
  threading.Thread(target=run_full_scan, daemon=True).start()
  return (
      "Manual 4H market scan (with $5M Volume Filter & Double-Sweep Filter)"
      " started! Check Telegram in 2 minutes.",
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
# 3. EXCHANGE & WATCHLIST SETUP ($5M+ Daily Volume Filter)
# =============================================================
exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
    "timeout": 30000,
})
TIMEFRAME = "4h"
MIN_DAILY_VOLUME = 5_000_000  # $5 Million USD Daily Volume Threshold


def get_all_futures_tokens():
  # Direct REST API fetch to bypass CCXT market loading rate-limits
  url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      )
  }

  try:
    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code == 200:
      data = response.json()
      filtered_tokens = []

      for item in data:
        symbol = item.get("symbol", "")

        # Target active USDT perpetual futures (e.g. BTCUSDT, ETHUSDT)
        # Exclude quarterly futures with numbers (e.g. BTCUSDT_240927)
        if symbol.endswith("USDT") and "_" not in symbol:
          quote_vol = float(item.get("quoteVolume", 0.0) or 0.0)

          if quote_vol >= MIN_DAILY_VOLUME:
            # Format cleanly for CCXT OHLCV calls (BTCUSDT -> BTC/USDT)
            base_coin = symbol[:-4]
            ccxt_symbol = f"{base_coin}/USDT"
            filtered_tokens.append(ccxt_symbol)

      print(
          f"Watchlist ready: {len(filtered_tokens)} unique tokens met > $5M"
          " volume filter."
      )

      if filtered_tokens:
        return filtered_tokens

  except Exception as e:
    print(f"Direct API fetch error: {e}")

  # Backup Fetch: Exchange Info Endpoint
  try:
    info_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    resp = requests.get(info_url, headers=headers, timeout=15).json()
    backup_tokens = [
        f"{s['baseAsset']}/USDT"
        for s in resp.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
        and "_" not in s.get("symbol", "")
    ]
    if backup_tokens:
      print(f"Backup Watchlist ready: {len(backup_tokens)} tokens.")
      return backup_tokens
  except Exception as backup_err:
    print(f"Backup endpoint error: {backup_err}")

  # Final fail-safe top liquid market list
  return [
      "BTC/USDT",
      "ETH/USDT",
      "SOL/USDT",
      "XRP/USDT",
      "ADA/USDT",
      "DOGE/USDT",
      "AVAX/USDT",
      "NEAR/USDT",
      "SUI/USDT",
      "LINK/USDT",
      "DOT/USDT",
      "MATIC/USDT",
      "BCH/USDT",
      "LTC/USDT",
      "APT/USDT",
      "PEPE/USDT",
      "SHIB/USDT",
      "FET/USDT",
      "ARBR/USDT",
      "OP/USDT",
  ]


# =============================================================
# 4. SWEEP PATTERN DETECTION LOGIC (WITH DOUBLE SWEEP FILTER)
# =============================================================
def check_liquidity_sweep(symbol):
  try:
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=4)
    if not ohlcv or len(ohlcv) < 4:
      return False

    prev_high = ohlcv[-3][2]
    prev_low = ohlcv[-3][3]

    closed_high = ohlcv[-2][2]
    closed_low = ohlcv[-2][3]
    closed_close = ohlcv[-2][4]

    # --- FILTER: IGNORE CANDLES THAT SWEEP BOTH HIGH AND LOW ---
    if (closed_high > prev_high) and (closed_low < prev_low):
      print(f"[SKIP] Double sweep (both high & low swept) on {symbol}")
      return False

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
      f"• *Tokens Filtered (> $5M Vol):* `{len(watchlist)}`\n"
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

      time_diff = (now_ist - target_dt).total_seconds()

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
