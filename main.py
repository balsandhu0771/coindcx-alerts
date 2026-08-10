import os
import threading
import time
from datetime import datetime, timedelta
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
      "Manual 4H Sweep + 15m MSS scan started! Check Telegram in 2 minutes.",
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
MIN_DAILY_VOLUME = 5_000_000  # $5 Million USD Volume Threshold

active_watchlists = {}
watchlist_lock = threading.Lock()


def get_all_futures_tokens():
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      )
  }

  try:
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    response = requests.get(url, headers=headers, timeout=10)

    if response.status_code == 200:
      data = response.json()
      filtered_tokens = set()

      for item in data:
        symbol = item.get("symbol", "")
        if symbol.endswith("USDT") and "_" not in symbol:
          quote_vol = float(item.get("quoteVolume", 0.0) or 0.0)
          if quote_vol >= MIN_DAILY_VOLUME:
            base_coin = symbol[:-4]
            filtered_tokens.add(f"{base_coin}/USDT")

      if filtered_tokens:
        result = list(filtered_tokens)
        print(f"Watchlist ready: {len(result)} unique tokens met > $5M volume.")
        return result
  except Exception as e:
    print(f"Primary ticker API failed: {e}")

  try:
    markets = exchange.load_markets(True)
    unique_futures = set()
    for symbol, market in markets.items():
      if (
          market.get("quote") == "USDT"
          and market.get("swap")
          and market.get("active", True)
          and ":" not in symbol
      ):
        unique_futures.add(symbol)

    if unique_futures:
      return list(unique_futures)
  except Exception as e:
    print(f"CCXT load_markets failed: {e}")

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
      "ARB/USDT",
      "OP/USDT",
      "TIA/USDT",
      "INJ/USDT",
      "RNDR/USDT",
      "STX/USDT",
      "FIL/USDT",
      "ETC/USDT",
      "TRX/USDT",
      "ATOM/USDT",
      "ICP/USDT",
      "AAVE/USDT",
  ]


# =============================================================
# 4. CLOSEST 15M PIVOT EXTRACTION WITH FULL-WINDOW MSS CHECK
# =============================================================
def extract_15m_levels(symbol, direction, target_4h_candle, prev_4h_level):
  """Extracts the CLOSEST 15m 3-candle pivot prior to the sweep peak/trough.

  Covering both -3 and -2 4H windows (~32 fifteen-minute candles). Enforces
  strict -3 boundary rule and checks ALL post-peak candles for MSS.
  """
  try:
    c_open_time = target_4h_candle[0]
    c_close_time = c_open_time + (4 * 60 * 60 * 1000)

    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=40)
    if not ohlcv_15m or len(ohlcv_15m) < 16:
      return None

    start_window_time = c_open_time - (4 * 60 * 60 * 1000)
    sweep_15m_candles = [
        c for c in ohlcv_15m if start_window_time <= c[0] < c_close_time
    ]

    if len(sweep_15m_candles) < 16:
      sweep_15m_candles = ohlcv_15m[-33:-1]

    latest_close = ohlcv_15m[-2][4]  # Close of last completed 15m candle

    if direction == "SHORT":
      # 1. Identify Highest High (H_max)
      max_high = -1.0
      peak_idx = 0
      for i, candle in enumerate(sweep_15m_candles):
        if candle[2] > max_high:
          max_high = candle[2]
          peak_idx = i

      # 2. Search BACKWARDS starting from peak_idx - 1 for CLOSEST 3-candle pivot low
      l_mss = None
      for i in range(peak_idx - 1, 0, -1):
        if i < len(sweep_15m_candles) - 1:
          curr_low = sweep_15m_candles[i][3]
          left_low = sweep_15m_candles[i - 1][3]
          right_low = sweep_15m_candles[i + 1][3]

          if curr_low < left_low and curr_low < right_low:
            l_mss = curr_low
            break

      if l_mss is None:
        if peak_idx > 0:
          l_mss = min(c[3] for c in sweep_15m_candles[:peak_idx])
        else:
          l_mss = sweep_15m_candles[0][3]

      # BOUNDARY RULE: Closest 15m swing low MUST be strictly lower than -3 4H High
      if l_mss >= prev_4h_level:
        print(
            f"[SKIP SHORT] {symbol} closest 15m low ({l_mss}) >= -3 4H High"
            f" ({prev_4h_level})."
        )
        return None

      # FIX: Check if ANY completed 15m candle after the peak has closed below l_mss
      post_peak_candles = sweep_15m_candles[peak_idx:]
      already_mss = any(
          c[4] < l_mss for c in post_peak_candles
      ) or latest_close < l_mss

      return {
          "h_max": max_high,
          "l_mss": l_mss,
          "already_mss": already_mss,
          "latest_close": latest_close,
      }

    elif direction == "LONG":
      # 1. Identify Lowest Low (L_min)
      min_low = float("inf")
      trough_idx = 0
      for i, candle in enumerate(sweep_15m_candles):
        if candle[3] < min_low:
          min_low = candle[3]
          trough_idx = i

      # 2. Search BACKWARDS starting from trough_idx - 1 for CLOSEST 3-candle pivot high
      h_mss = None
      for i in range(trough_idx - 1, 0, -1):
        if i < len(sweep_15m_candles) - 1:
          curr_high = sweep_15m_candles[i][2]
          left_high = sweep_15m_candles[i - 1][2]
          right_high = sweep_15m_candles[i + 1][2]

          if curr_high > left_high and curr_high > right_high:
            h_mss = curr_high
            break

      if h_mss is None:
        if trough_idx > 0:
          h_mss = max(c[2] for c in sweep_15m_candles[:trough_idx])
        else:
          h_mss = sweep_15m_candles[0][2]

      # BOUNDARY RULE: Closest 15m swing high MUST be strictly higher than -3 4H Low
      if h_mss <= prev_4h_level:
        print(
            f"[SKIP LONG] {symbol} closest 15m high ({h_mss}) <= -3 4H Low"
            f" ({prev_4h_level})."
        )
        return None

      # FIX: Check if ANY completed 15m candle after the trough has closed above h_mss
      post_trough_candles = sweep_15m_candles[trough_idx:]
      already_mss = any(
          c[4] > h_mss for c in post_trough_candles
      ) or latest_close > h_mss

      return {
          "l_min": min_low,
          "h_mss": h_mss,
          "already_mss": already_mss,
          "latest_close": latest_close,
      }

  except Exception as e:
    print(f"Error extracting 15m levels for {symbol}: {e}")
    return None


# =============================================================
# 5. 4H SWEEP CHECK & WATCHLIST ROUTING
# =============================================================
def check_liquidity_sweep(symbol):
  try:
    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=4)
    if not ohlcv_4h or len(ohlcv_4h) < 4:
      return False

    prev_high = ohlcv_4h[-3][2]  # -3 4H High
    prev_low = ohlcv_4h[-3][3]  # -3 4H Low

    sweep_candle_4h = ohlcv_4h[-2]  # The -2 4H candle
    closed_high = sweep_candle_4h[2]
    closed_low = sweep_candle_4h[3]
    closed_close = sweep_candle_4h[4]

    # Filter: Double Sweeps Ignore
    if (closed_high > prev_high) and (closed_low < prev_low):
      return False

    # 1. BEARISH SWEEP (SHORT) SETUP
    if closed_high > prev_high and closed_close < prev_high:
      levels = extract_15m_levels(
          symbol, "SHORT", sweep_candle_4h, prev_4h_level=prev_high
      )
      if not levels:
        return False

      risk = levels["h_max"] - levels["l_mss"]
      reward = levels["l_mss"] - prev_low
      rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

      if levels["already_mss"]:
        msg = (
            f"🚨 *BEARISH 4H SWEEP + 15M MSS (SHORT)* 🚨\n\n"
            f"*Token:* `{symbol}`\n"
            f"*Current Price:* `${levels['latest_close']}`\n"
            f"*15m Swing Low (Entry):* `${levels['l_mss']}`\n"
            f"*Stop Loss (H_max):* `${levels['h_max']}` (Prev High:"
            f" `${prev_high}`)\n"
            f"*Take Profit (Prev Low):* `${prev_low}`\n"
            f"*Est. Risk-to-Reward (R:R):* `1:{rr_ratio}`\n\n"
            f"💡 *Setup:* 4H high swept and 15m structure already shifted"
            f" bearish!"
        )
        print(f"[MATCH SHORT IMMEDIATE] {symbol}")
        send_telegram_alert(msg)
        return True
      else:
        with watchlist_lock:
          active_watchlists[symbol] = {
              "direction": "SHORT",
              "h_max": levels["h_max"],
              "l_mss": levels["l_mss"],
              "prev_low": prev_low,
              "expires_at": datetime.now(IST) + timedelta(hours=4),
          }
        print(
            f"[ADDED TO WATCHLIST SHORT] {symbol} | H_max: {levels['h_max']} |"
            f" L_mss: {levels['l_mss']} | R:R: 1:{rr_ratio}"
        )
        return False

    # 2. BULLISH SWEEP (LONG) SETUP
    elif (
        (closed_low < prev_low)
        and (closed_close > prev_low)
        and (closed_close < prev_high)
    ):
      levels = extract_15m_levels(
          symbol, "LONG", sweep_candle_4h, prev_4h_level=prev_low
      )
      if not levels:
        return False

      risk = levels["h_mss"] - levels["l_min"]
      reward = prev_high - levels["h_mss"]
      rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

      if levels["already_mss"]:
        msg = (
            f"🚨 *BULLISH 4H SWEEP + 15M MSS (LONG)* 🚨\n\n"
            f"*Token:* `{symbol}`\n"
            f"*Current Price:* `${levels['latest_close']}`\n"
            f"*15m Swing High (Entry):* `${levels['h_mss']}`\n"
            f"*Stop Loss (L_min):* `${levels['l_min']}` (Prev Low:"
            f" `${prev_low}`)\n"
            f"*Take Profit (Prev High):* `${prev_high}`\n"
            f"*Est. Risk-to-Reward (R:R):* `1:{rr_ratio}`\n\n"
            f"💡 *Setup:* 4H low swept and 15m structure already shifted"
            f" bullish!"
        )
        print(f"[MATCH LONG IMMEDIATE] {symbol}")
        send_telegram_alert(msg)
        return True
      else:
        with watchlist_lock:
          active_watchlists[symbol] = {
              "direction": "LONG",
              "l_min": levels["l_min"],
              "h_mss": levels["h_mss"],
              "prev_high": prev_high,
              "expires_at": datetime.now(IST) + timedelta(hours=4),
          }
        print(
            f"[ADDED TO WATCHLIST LONG] {symbol} | L_min: {levels['l_min']} |"
            f" H_mss: {levels['h_mss']} | R:R: 1:{rr_ratio}"
        )
        return False

    return False

  except Exception as e:
    print(f"Error checking {symbol}: {e}")
    return False


# =============================================================
# 6. 15-MINUTE BACKGROUND WATCHLIST MONITOR
# =============================================================
def check_15m_watchlists():
  with watchlist_lock:
    if not active_watchlists:
      return
    symbols_to_check = list(active_watchlists.items())

  print(
      f"\n--- [15m Monitor] Checking {len(symbols_to_check)} pending setups"
      " ---"
  )
  now_time = datetime.now(IST)
  symbols_to_remove = []

  for symbol, data in symbols_to_check:
    try:
      if now_time > data["expires_at"]:
        print(f"[EXPIRED] {symbol} setup expired after 4 hours.")
        symbols_to_remove.append(symbol)
        continue

      ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=3)
      if not ohlcv_15m or len(ohlcv_15m) < 2:
        continue

      latest_high = ohlcv_15m[-1][2]
      latest_low = ohlcv_15m[-1][3]
      latest_close = ohlcv_15m[-2][4]  # Last completed 15m candle close

      # BEARISH WATCHLIST CHECK
      if data["direction"] == "SHORT":
        if latest_close > data["h_max"]:
          print(
              f"[INVALIDATED SHORT] {symbol} closed above H_max"
              f" ({data['h_max']})."
          )
          symbols_to_remove.append(symbol)
          continue

        if latest_close < data["l_mss"]:
          risk = data["h_max"] - data["l_mss"]
          reward = data["l_mss"] - data["prev_low"]
          rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

          msg = (
              f"🚨 *BEARISH 4H SWEEP + 15M MSS CONFIRMED* 🚨\n\n"
              f"*Token:* `{symbol}`\n"
              f"*Current 15m Close:* `${latest_close}`\n"
              f"*15m Swing Low (Entry):* `${data['l_mss']}`\n"
              f"*Stop Loss (H_max):* `${data['h_max']}`\n"
              f"*Take Profit (Prev Low):* `${data['prev_low']}`\n"
              f"*Est. Risk-to-Reward (R:R):* `1:{rr_ratio}`\n\n"
              f"💡 *Setup:* Price held below 4H peak and just confirmed 15m"
              f" MSS!"
          )
          print(f"[TRIGGER SHORT 15M] {symbol}")
          send_telegram_alert(msg)
          symbols_to_remove.append(symbol)
          continue

      # BULLISH WATCHLIST CHECK
      elif data["direction"] == "LONG":
        if latest_close < data["l_min"]:
          print(
              f"[INVALIDATED LONG] {symbol} closed below L_min"
              f" ({data['l_min']})."
          )
          symbols_to_remove.append(symbol)
          continue

        if latest_close > data["h_mss"]:
          risk = data["h_mss"] - data["l_min"]
          reward = data["prev_high"] - data["h_mss"]
          rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

          msg = (
              f"🚨 *BULLISH 4H SWEEP + 15M MSS CONFIRMED* 🚨\n\n"
              f"*Token:* `{symbol}`\n"
              f"*Current 15m Close:* `${latest_close}`\n"
              f"*15m Swing High (Entry):* `${data['h_mss']}`\n"
              f"*Stop Loss (L_min):* `${data['l_min']}`\n"
              f"*Take Profit (Prev High):* `${data['prev_high']}`\n"
              f"*Est. Risk-to-Reward (R:R):* `1:{rr_ratio}`\n\n"
              f"💡 *Setup:* Price held above 4H trough and just confirmed 15m"
              f" MSS!"
          )
          print(f"[TRIGGER LONG 15M] {symbol}")
          send_telegram_alert(msg)
          symbols_to_remove.append(symbol)
          continue

    except Exception as e:
      print(f"Error monitoring watchlist for {symbol}: {e}")

  with watchlist_lock:
    for sym in symbols_to_remove:
      active_watchlists.pop(sym, None)


def run_full_scan():
  watchlist = get_all_futures_tokens()
  print(f"\n--- Starting Full Scan across {len(watchlist)} tokens ---")

  immediate_alerts = 0
  for symbol in watchlist:
    try:
      matched = check_liquidity_sweep(symbol)
      if matched:
        immediate_alerts += 1
      time.sleep(0.12)
    except Exception as e:
      print(f"Error in scanning loop for {symbol}: {e}")
      time.sleep(0.5)

  with watchlist_lock:
    pending_count = len(active_watchlists)

  summary_msg = (
      f"🔍 *4H Sweep + 15M MSS Scan Complete*\n"
      f"• *Tokens Scanned:* `{len(watchlist)}`\n"
      f"• *Immediate MSS Alerts Fired:* `{immediate_alerts}`\n"
      f"• *Added to Active 15m Watchlist:* `{pending_count}`"
  )
  send_telegram_alert(summary_msg)
  print(f"Scan complete across {len(watchlist)} quality tokens!")

  # Immediately run watchlist check right after main scan completes
  if pending_count > 0:
    threading.Thread(target=check_15m_watchlists, daemon=True).start()


# =============================================================
# 7. SCHEDULER & MAIN EXECUTION
# =============================================================
def start_scheduler():
  print("=== Starting 24/7 Market Monitor ===")
  send_telegram_alert(
      "IST Bot Live! Full-Window MSS Scanning, Post-Scan Watchlist Re-Check &"
      " Robust Pivot Extraction Enabled."
  )

  last_executed_slot = None
  last_15m_min = -1

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

    curr_min = now_ist.minute
    if curr_min in [1, 16, 31, 46] and curr_min != last_15m_min:
      threading.Thread(target=check_15m_watchlists, daemon=True).start()
      last_15m_min = curr_min

    time.sleep(15)


if __name__ == "__main__":
  scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
  scheduler_thread.start()

  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)
