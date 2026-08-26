import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone
import requests
import ccxt
from flask import Flask, jsonify

# =====================================================================
# 1. LOGGING & SYSTEM CONFIGURATION
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "8642933768:AAH3afnXGmaAplHDar9u4uwJ5IZz0M7y7fs"
TELEGRAM_CHAT_IDS = [7203290966, 630462102]
BOOT_TIME = datetime.now(timezone.utc)

raw_port = os.environ.get("PORT", "8080")
try:
    PORT = int(raw_port)
except Exception:
    PORT = 8080

exchange = ccxt.binanceusdm({
    'enableRateLimit': True,
    'rateLimit': 100,
    'options': {
        'defaultType': 'future',
        'adjustForTimeDifference': True
    }
})

active_watchlists = {}     # {symbol: setup_data_dict}
processed_sweeps = set()   # set of (symbol, 4h_candle_timestamp)
scan_history = []
is_scan_running = False
last_scan_epoch = ""

# =====================================================================
# 2. TELEGRAM DISPATCHER
# =====================================================================
def send_telegram_alert(message):
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code != 200:
                logger.error(f"[TELEGRAM FAIL] {chat_id} | Status: {res.status_code} | Body: {res.text}")
        except Exception as e:
            logger.error(f"[TELEGRAM EXCEPTION] Failed to dispatch to {chat_id}: {e}")

# =====================================================================
# 3. TOP 150 LIQUID CRYPTO FUTURES (RATE-LIMIT PROTECTED)
# =====================================================================
def get_all_futures_symbols():
    """
    Fetches Binance Futures volume rankings and returns top 150 crypto perpetuals.
    """
    try:
        tickers = exchange.fetch_tickers()
        valid_pairs = []
        equity_blacklist = {
            "APPLE", "ADBE", "ASTS", "NVDA", "TSLA", "MSFT", "AMZN", 
            "GOOGL", "META", "COIN", "PLTR", "HOOD", "AMD", "NFLX", "BABA"
        }

        for symbol, data in tickers.items():
            if symbol.endswith('/USDT:USDT') or (symbol.endswith('/USDT') and ':' not in symbol):
                base = symbol.split('/')[0]
                if base.upper() in equity_blacklist or base.isdigit():
                    continue

                quote_vol = float(data.get('quoteVolume', 0) or 0)
                formatted = symbol if ':' in symbol else f"{symbol}:USDT"
                valid_pairs.append({'symbol': formatted, 'volume': quote_vol})

        valid_pairs.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [x['symbol'] for x in valid_pairs[:150]]

        if top_symbols:
            logger.info(f"Loaded top {len(top_symbols)} liquid futures pairs.")
            return top_symbols

    except Exception as e:
        logger.error(f"Error loading ranked futures tickers: {e}")

    try:
        markets = exchange.load_markets()
        symbols = [
            s if ':' in s else f"{s}:USDT"
            for s, m in markets.items()
            if m.get('active', True) and m.get('quote') == 'USDT' and (m.get('swap', False) or m.get('linear', False))
        ]
        return symbols[:150]
    except Exception as e:
        logger.error(f"Fallback symbol loader error: {e}")
        return []

# =====================================================================
# 4. 15-MINUTE MARKET STRUCTURE SHIFT (MSS) ENGINE
# =====================================================================
def extract_15m_levels(symbol, setup_type, h_ref, l_ref):
    try:
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=24)
        if len(ohlcv_15m) < 17:
            return None

        window_15m = ohlcv_15m[-17:-1]

        if setup_type == "LONG":
            l_min = min(c[3] for c in window_15m)
            min_idx = [i for i, c in enumerate(window_15m) if c[3] == l_min][0]

            h_mss = None
            for i in range(min_idx, -1, -1):
                c = window_15m[i]
                if h_mss is None or c[2] > h_mss:
                    h_mss = c[2]

            if h_mss is None:
                return None

            target = h_ref
            risk = h_mss - l_min
            reward = target - h_mss

            if risk <= 0:
                return None

            rr_ratio = round(reward / risk, 2) if reward > 0 else 1.0
            return {
                "l_min": l_min,
                "h_mss": h_mss,
                "l_ref": l_ref,
                "h_ref": h_ref,
                "entry": h_mss,
                "stop_loss": l_min,
                "target": target,
                "rr_ratio": max(rr_ratio, 0.5)
            }

        elif setup_type == "SHORT":
            h_max = max(c[2] for c in window_15m)
            max_idx = [i for i, c in enumerate(window_15m) if c[2] == h_max][0]

            l_mss = None
            for i in range(max_idx, -1, -1):
                c = window_15m[i]
                if l_mss is None or c[3] < l_mss:
                    l_mss = c[3]

            if l_mss is None:
                return None

            target = l_ref
            risk = h_max - l_mss
            reward = l_mss - target

            if risk <= 0:
                return None

            rr_ratio = round(reward / risk, 2) if reward > 0 else 1.0
            return {
                "h_max": h_max,
                "l_mss": l_mss,
                "h_ref": h_ref,
                "l_ref": l_ref,
                "entry": l_mss,
                "stop_loss": h_max,
                "target": target,
                "rr_ratio": max(rr_ratio, 0.5)
            }

    except Exception as e:
        logger.error(f"[{symbol}] Error in extract_15m_levels: {e}")
        return None

# =====================================================================
# 5. 4H LIQUIDITY SWEEP EVALUATOR
# =====================================================================
def check_liquidity_sweep(symbol):
    try:
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=5)
        if len(ohlcv_4h) < 4:
            return None

        c_ref = ohlcv_4h[-3]
        c_trig = ohlcv_4h[-2]

        h_ref, l_ref = c_ref[2], c_ref[3]
        h_trig, l_trig, c_trig_close = c_trig[2], c_trig[3], c_trig[4]
        trig_timestamp = c_trig[0]

        long_sweep = (l_trig < l_ref) and (c_trig_close > l_ref) and (h_trig <= h_ref)
        short_sweep = (h_trig > h_ref) and (c_trig_close < h_ref) and (l_trig >= l_ref)

        if not (long_sweep or short_sweep):
            return None

        setup_type = "LONG" if long_sweep else "SHORT"
        levels = extract_15m_levels(symbol, setup_type, h_ref, l_ref)
        if not levels:
            return None

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
            "candle_timestamp": trig_timestamp,
            "detected_at": datetime.now(timezone.utc)
        }

    except Exception as e:
        logger.error(f"[{symbol}] Liquidity Sweep Exception: {e}")
        return None

# =====================================================================
# 6. MARKET SCAN WORKERS
# =====================================================================
def run_full_scan():
    global is_scan_running
    if is_scan_running:
        return

    is_scan_running = True
    scan_start = time.time()
    logger.info("=== Starting Rate-Limited 4H Liquidity Sweep Scan ===")
    try:
        symbols = get_all_futures_symbols()
        found_setups = []

        for sym in symbols:
            res = check_liquidity_sweep(sym)
            if res:
                sweep_id = (sym, res["candle_timestamp"])
                if sweep_id not in processed_sweeps:
                    found_setups.append(res)
                    processed_sweeps.add(sweep_id)
                active_watchlists[sym] = res
            time.sleep(0.08)  # Safe pace to avoid rate limits

        elapsed = round(time.time() - scan_start, 1)

        msg = "⚡ *4H LIQUIDITY SWEEP SCAN COMPLETE* ⚡\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📊 *Total Pairs Evaluated:* `{len(symbols)}`\n"
        msg += f"🎯 *Setups Identified:* `{len(found_setups)}`\n"
        msg += f"⏱ *Scan Duration:* `{elapsed}s`\n"
        msg += f"⏰ *Time (UTC):* `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}`\n"

        if found_setups:
            for s in found_setups:
                sym_clean = s['symbol'].split(':')[0]
                lvl = s['levels']
                status_icon = "🟢" if s['type'] == "LONG" else "🔴"
                msg += f"\n━━━━━━━━━━━━━━━━━━━━\n"
                msg += f"{status_icon} *{sym_clean}* | *{s['type']} SWEEP*\n"
                msg += f"• *Entry Level (15m MSS):* `{lvl['entry']}`\n"
                msg += f"• *Invalidation (SL):* `{lvl['stop_loss']}`\n"
                msg += f"• *Target (4H Liquidity):* `{lvl['target']}`\n"
                msg += f"• *Risk : Reward:* `1 : {lvl['rr_ratio']}`\n"
                msg += f"• *MSS Triggered:* `{'YES (Immediate)' if s['already_mss'] else 'NO (Monitoring 15m)'}`"
        else:
            msg += "\nNo fresh 4H sweeps currently meeting all structural parameters."

        send_telegram_alert(msg)

        scan_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pairs_evaluated": len(symbols),
            "setups_found": len(found_setups),
            "duration_seconds": elapsed
        })
        if len(scan_history) > 20:
            scan_history.pop(0)

        logger.info(f"Scan finished in {elapsed}s with {len(found_setups)} setups dispatched.")

    except Exception as e:
        logger.error(f"Scan Execution Exception: {e}")
    finally:
        is_scan_running = False

def run_15m_check():
    if not active_watchlists:
        return

    logger.info(f"Evaluating 15m Watchlist ({len(active_watchlists)} active setups)...")
    to_remove = []

    for sym, data in list(active_watchlists.items()):
        try:
            ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=3)
            last_closed = ohlcv[-2]
            c_close = last_closed[4]
            levels = data['levels']
            sym_clean = sym.split(':')[0]

            if data['type'] == "LONG":
                if last_closed[3] < levels['l_min']:
                    to_remove.append(sym)
                    continue

                if c_close > levels['h_mss']:
                    send_telegram_alert(
                        f"🚀 *LONG MSS CONFIRMED: {sym_clean}*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"• *Entry Trigger:* `{levels['entry']}`\n"
                        f"• *Stop Loss:* `{levels['stop_loss']}`\n"
                        f"• *Target (4H High):* `{levels['target']}`\n"
                        f"• *Genuine R:R:* `1 : {levels['rr_ratio']}`\n"
                        "• *Status:* Confirmed 15m Candle Close"
                    )
                    to_remove.append(sym)

            elif data['type'] == "SHORT":
                if last_closed[2] > levels['h_max']:
                    to_remove.append(sym)
                    continue

                if c_close < levels['l_mss']:
                    send_telegram_alert(
                        f"🔻 *SHORT MSS CONFIRMED: {sym_clean}*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"• *Entry Trigger:* `{levels['entry']}`\n"
                        f"• *Stop Loss:* `{levels['stop_loss']}`\n"
                        f"• *Target (4H Low):* `{levels['target']}`\n"
                        f"• *Genuine R:R:* `1 : {levels['rr_ratio']}`\n"
                        "• *Status:* Confirmed 15m Candle Close"
                    )
                    to_remove.append(sym)

        except Exception as e:
            logger.error(f"Error checking 15m candle for {sym}: {e}")

    for sym in to_remove:
        active_watchlists.pop(sym, None)

# =====================================================================
# 7. 24/7 BACKGROUND SCHEDULING THREAD
# =====================================================================
def scheduler_loop():
    global last_scan_epoch
    logger.info("Background scheduler thread active.")
    while True:
        now = datetime.now(timezone.utc)
        if now.minute in [1, 16, 31, 46] and now.second < 20:
            current_epoch_key = f"{now.strftime('%Y%m%d')}_{now.hour}_{now.minute}"

            if now.hour in [0, 4, 8, 12, 16, 20] and now.minute == 1:
                if last_scan_epoch != current_epoch_key:
                    last_scan_epoch = current_epoch_key
                    run_full_scan()
            else:
                if last_scan_epoch != current_epoch_key:
                    last_scan_epoch = current_epoch_key
                    run_15m_check()
            time.sleep(25)
        time.sleep(5)

# =====================================================================
# 8. FLASK SERVER & WEBHOOK DIAGNOSTICS
# =====================================================================
app = Flask(__name__)

@app.route('/')
def home():
    uptime = str(datetime.now(timezone.utc) - BOOT_TIME).split('.')[0]
    return jsonify({
        "status": "online",
        "service": "crypto-alerts-24-7",
        "active_watchlists_count": len(active_watchlists),
        "uptime": uptime,
        "server_time_utc": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    })

@app.route('/health')
def health_check():
    uptime = str(datetime.now(timezone.utc) - BOOT_TIME).split('.')[0]
    return jsonify({
        "status": "healthy",
        "service": "crypto-trading-bot",
        "uptime": uptime,
        "active_watchlists": active_watchlists,
        "recent_scans": scan_history
    })

@app.route('/trigger-scan')
def trigger_scan():
    if is_scan_running:
        return "Scan already running. Please wait for completion."
    send_telegram_alert("⚡ *Manual scan initiated via Webhook...* Evaluating top 150 liquid pairs now.")
    threading.Thread(target=run_full_scan, daemon=True).start()
    return "Manual 4H Sweep + 15m MSS scan started! Check Telegram in 1-2 minutes."

@app.route('/debug-token/<path:symbol>')
def debug_token_endpoint(symbol):
    clean = symbol.upper().replace('-', '/').split(':')[0]
    if not clean.endswith('/USDT') and not clean.endswith('USDT'):
        clean += '/USDT'
    elif clean.endswith('USDT') and '/' not in clean:
        clean = clean.replace('USDT', '/USDT')

    formatted = f"{clean}:USDT"

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

# =====================================================================
# 9. PROCESS ENTRYPOINT
# =====================================================================
if __name__ == '__main__':
    try:
        exchange.load_markets()
    except Exception as e:
        logger.warning(f"Initial market load exception: {e}")

    send_telegram_alert(
        "🚀 *Crypto Trading Bot is Online & Active on Render!* \n\n"
        "• *Engine:* 4H Sweep + 15m MSS (Rate-Limit Protected)\n"
        "• *Target System:* Dynamic 4H Opposing Liquidity\n"
        "• *Coverage:* Top 150 Liquid USDT Crypto Perpetuals\n"
        "• *Status:* 24/7 Monitoring Initialized"
    )

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    logger.info("=== 24/7 Market Monitor Initialized Successfully ===")
    app.run(host='0.0.0.0', port=PORT)
