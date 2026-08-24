import os
import time
import threading
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask, jsonify

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8642933768:AAH3afnXGmaAplHDar9u4uwJ5IZz0M7y7fs"
TELEGRAM_CHAT_IDS = [7203290966, 630462102]
PORT = int(os.environ.get("PORT", 8080))

# Initialize Binance Futures Client
exchange = ccxt.binanceusdm({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# Global state for tracking setups
active_watchlists = {}  # {symbol: {data}}

# ---------------------------------------------------------
# TELEGRAM NOTIFIER
# ---------------------------------------------------------
def send_telegram_alert(message):
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"Failed to send Telegram message to {chat_id}: {e}")

# ---------------------------------------------------------
# STRATEGY LOGIC: 4H SWEEP & 15M MSS
# ---------------------------------------------------------
def extract_15m_levels(symbol, setup_type, h_ref, l_ref, c_trig_high, c_trig_low):
    """
    Extracts 15m MSS structural level and verifies boundaries.
    """
    try:
        # Fetch last 20 15m candles
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
        if len(ohlcv_15m) < 16:
            return None
        
        # 4H window is roughly the 16 candles prior to the current forming candle
        window_15m = ohlcv_15m[-17:-1]

        if setup_type == "LONG":
            # Lowest point during the 4H sweep window
            l_min = min(c[3] for c in window_15m)
            min_idx = [i for i, c in enumerate(window_15m) if c[3] == l_min][0]
            
            # Find closest swing high before/at the low
            h_mss = None
            for i in range(min_idx, -1, -1):
                c = window_15m[i]
                if h_mss is None or c[2] > h_mss:
                    h_mss = c[2]
            
            if h_mss is None:
                return None

            # Boundary filter check: MSS trigger must sit logically above reference low
            if h_mss <= l_ref:
                print(f"[{symbol}] SKIP LONG: 15m MSS level ({h_mss}) <= Reference Low ({l_ref})")
                return None

            return {"l_min": l_min, "h_mss": h_mss, "l_ref": l_ref}

        elif setup_type == "SHORT":
            # Highest point during the 4H sweep window
            h_max = max(c[2] for c in window_15m)
            max_idx = [i for i, c in enumerate(window_15m) if c[2] == h_max][0]
            
            # Find closest swing low before/at the high
            l_mss = None
            for i in range(max_idx, -1, -1):
                c = window_15m[i]
                if l_mss is None or c[3] < l_mss:
                    l_mss = c[3]

            if l_mss is None:
                return None

            # Boundary filter check: MSS trigger must sit logically below reference high
            if l_mss >= h_ref:
                print(f"[{symbol}] SKIP SHORT: 15m MSS level ({l_mss}) >= Reference High ({h_ref})")
                return None

            return {"h_max": h_max, "l_mss": l_mss, "h_ref": h_ref}

    except Exception as e:
        print(f"Error in extract_15m_levels for {symbol}: {e}")
        return None

def check_liquidity_sweep(symbol):
    try:
        # Fetch last 5 4H candles
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=5)
        if len(ohlcv_4h) < 4:
            return None

        # [-3] = Reference candle, [-2] = Trigger/Sweep candle just closed, [-1] = Current forming candle
        c_ref = ohlcv_4h[-3]
        c_trig = ohlcv_4h[-2]

        h_ref, l_ref = c_ref[2], c_ref[3]
        h_trig, l_trig, c_trig_close = c_trig[2], c_trig[3], c_trig[4]

        # 1. LONG SWEEP: Low swept, Closed back inside reference, did not sweep opposite high
        long_sweep = (l_trig < l_ref) and (c_trig_close > l_ref) and (h_trig <= h_ref)
        
        # 2. SHORT SWEEP: High swept, Closed back inside reference, did not sweep opposite low
        short_sweep = (h_trig > h_ref) and (c_trig_close < h_ref) and (l_trig >= l_ref)

        if not (long_sweep or short_sweep):
            return None

        setup_type = "LONG" if long_sweep else "SHORT"
        levels = extract_15m_levels(symbol, setup_type, h_ref, l_ref, h_trig, l_trig)
        if not levels:
            return None

        # Check if MSS has already triggered on recent 15m candles
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=10)
        recent_15m = ohlcv_15m[-5:-1]
        
        already_mss = False
        if setup_type == "LONG":
            already_mss = any(c[4] > levels['h_mss'] for c in recent_15m)
        else:
            already_mss = any(c[4] < levels['l_mss'] for c in recent_15m)

        return {
            "symbol": symbol,
            "type": setup_type,
            "levels": levels,
            "already_mss": already_mss,
            "detected_at": datetime.now(timezone.utc)
        }

    except Exception as e:
        print(f"Error checking {symbol}: {e}")
        return None

# ---------------------------------------------------------
# SCAN WORKERS
# ---------------------------------------------------------
def run_full_scan():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Starting Full 4H Sweep Scan...")
    try:
        markets = exchange.load_markets()
        symbols = [s for s, m in markets.items() if m['active'] and s.endswith('/USDT') and m.get('contract', True)]
        
        # Filter top ~120 active pairs
        symbols = symbols[:120]
        
        found_setups = []
        for sym in symbols:
            res = check_liquidity_sweep(sym)
            if res:
                found_setups.append(res)
                active_watchlists[sym] = res
            time.sleep(0.05)

        msg = f"🔍 *4H Market Scan Complete*\n\nTotal Evaluated: `{len(symbols)}` pairs\nSetups Detected: `{len(found_setups)}`\n"
        if found_setups:
            for s in found_setups:
                msg += f"\n• *{s['symbol']}* ({s['type']}) | Immediate MSS: `{s['already_mss']}`"
        else:
            msg += "\nNo high-confluence 4H sweeps found matching entry parameters."
        
        send_telegram_alert(msg)
        print("Scan finished and summary dispatched.")
    except Exception as e:
        print(f"Scan execution error: {e}")

def run_15m_check():
    if not active_watchlists:
        return
    
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Checking 15m Watchlist ({len(active_watchlists)} active)...")
    to_remove = []

    for sym, data in list(active_watchlists.items()):
        try:
            ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=3)
            last_closed = ohlcv[-2]
            c_close = last_closed[4]
            levels = data['levels']

            if data['type'] == "LONG":
                # Invalidation: broke below sweep low
                if last_closed[3] < levels['l_min']:
                    to_remove.append(sym)
                    continue
                # MSS Trigger: closed above swing high
                if c_close > levels['h_mss']:
                    send_telegram_alert(f"🚀 *LONG MSS CONFIRMED: {sym}*\n\nPrice broke & closed above 15m swing high: `{levels['h_mss']}`\nStop Invalid: `{levels['l_min']}`")
                    to_remove.append(sym)

            elif data['type'] == "SHORT":
                # Invalidation: broke above sweep high
                if last_closed[2] > levels['h_max']:
                    to_remove.append(sym)
                    continue
                # MSS Trigger: closed below swing low
                if c_close < levels['l_mss']:
                    send_telegram_alert(f"🔻 *SHORT MSS CONFIRMED: {sym}*\n\nPrice broke & closed below 15m swing low: `{levels['l_mss']}`\nStop Invalid: `{levels['h_max']}`")
                    to_remove.append(sym)

        except Exception as e:
            print(f"Error checking 15m status for {sym}: {e}")

    for sym in to_remove:
        active_watchlists.pop(sym, None)

# ---------------------------------------------------------
# BACKGROUND SCHEDULER
# ---------------------------------------------------------
def scheduler_loop():
    while True:
        now = datetime.now(timezone.utc)
        # Check every 15m close (:01, :16, :31, :46)
        if now.minute in [1, 16, 31, 46] and now.second < 20:
            # Check 4H close windows (00:01, 04:01, 08:01, 12:01, 16:01, 20:01 UTC)
            if now.hour in [0, 4, 8, 12, 16, 20] and now.minute == 1:
                run_full_scan()
            else:
                run_15m_check()
            time.sleep(60)
        time.sleep(5)

# ---------------------------------------------------------
# FLASK WEB APP & DEBUG ROUTES
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "service": "coindcx-alerts",
        "active_watchlists": len(active_watchlists),
        "server_time_utc": datetime.now(timezone.utc).isoformat()
    })

@app.route('/trigger-scan')
def trigger_scan():
    threading.Thread(target=run_full_scan, daemon=True).start()
    return "Manual 4H Sweep + 15m MSS scan started! Check Telegram in 2 minutes."

@app.route('/debug-token/<path:symbol>')
def debug_token_endpoint(symbol):
    formatted = symbol.upper().replace('-', '/')
    if not formatted.endswith('USDT'):
        formatted += '/USDT'
    if '/' not in formatted:
        formatted = formatted.replace('USDT', '/USDT')

    try:
        ohlcv_4h = exchange.fetch_ohlcv(formatted, timeframe='4h', limit=6)
        c_ref = ohlcv_4h[-3]
        c_trig = ohlcv_4h[-2]

        h_ref, l_ref = c_ref[2], c_ref[3]
        h_trig, l_trig, c_trig_close = c_trig[2], c_trig[3], c_trig[4]

        long_sweep = bool((l_trig < l_ref) and (c_trig_close > l_ref) and (h_trig <= h_ref))
        short_sweep = bool((h_trig > h_ref) and (c_trig_close < h_ref) and (l_trig >= l_ref))

        eval_result = check_liquidity_sweep(formatted)

        return jsonify({
            "symbol": formatted,
            "4h_reference_candle_3": {"high": h_ref, "low": l_ref},
            "4h_trigger_candle_2": {"high": h_trig, "low": l_trig, "close": c_trig_close},
            "conditions_met": {
                "long_sweep": long_sweep,
                "short_sweep": short_sweep
            },
            "passed_full_evaluation": bool(eval_result),
            "evaluation_details": eval_result
        })
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    # Start scheduler daemon thread
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    print("=== Starting 24/7 Market Monitor ===")
    app.run(host='0.0.0.0', port=PORT)
