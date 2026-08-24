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

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = "8642933768:AAH3afnXGmaAplHDar9u4uwJ5IZz0M7y7fs"
TELEGRAM_CHAT_IDS = [7203290966, 630462102]

# Server Boot Time Tracker for Telemetry
BOOT_TIME = datetime.now(timezone.utc)

# Robust Port Extraction for Render Hosting
raw_port = os.environ.get("PORT", "8080")
try:
    PORT = int(raw_port)
except Exception:
    PORT = 8080

# CoinDCX Public Endpoint
COINDCX_MARKETS_URL = "https://api.coindcx.com/exchange/v1/markets_details"

# Initialize CCXT Binance USDⓈ-M Futures Exchange Client
exchange = ccxt.binanceusdm({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future',
        'adjustForTimeDifference': True
    }
})

# In-memory storage for active setups being monitored on 15m candles
active_watchlists = {}     # Format: {symbol: setup_data_dict}
processed_sweeps = set()   # Format: set of (symbol, 4h_candle_timestamp) to avoid duplicate alerts
scan_history = []          # Stores recent scan telemetry logs

# =====================================================================
# 2. TELEGRAM DISPATCHER SYSTEM
# =====================================================================
def send_telegram_alert(message):
    """
    Sends a formatted Markdown message to all configured Telegram recipient IDs.
    """
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
                logger.error(
                    f"[TELEGRAM FAIL] Chat ID: {chat_id} | "
                    f"Status: {res.status_code} | Body: {res.text}"
                )
            else:
                logger.info(f"[TELEGRAM SUCCESS] Message dispatched to {chat_id}")
        except Exception as e:
            logger.error(f"[TELEGRAM EXCEPTION] Failed to dispatch message to {chat_id}: {e}")

# =====================================================================
# 3. DYNAMIC SYMBOL FETCHER & COINDCX VOLUME MAPPER
# =====================================================================
def fetch_coindcx_top_pairs(limit=120):
    """
    Fetches market data from CoinDCX API, filters strictly to genuine crypto perpetuals,
    and maps tickers to CCXT Binance Futures format.
    """
    try:
        logger.info("Fetching active market data from CoinDCX public API...")
        response = requests.get(COINDCX_MARKETS_URL, timeout=10)
        
        if response.status_code != 200:
            logger.warning(
                f"CoinDCX API returned status {response.status_code}. "
                "Falling back to Binance native markets."
            )
            return []

        data = response.json()
        usdt_pairs = []

        # Stock/Synthetic blacklists to eliminate non-crypto tickers
        equity_blacklist = {"APPLE", "ADBE", "ASTS", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL", "META"}

        for item in data:
            target_currency = item.get("target_currency_short_name", "")
            base_currency = item.get("base_currency_short_name", "")
            status = item.get("status", "")

            # Filter active USDT crypto pairs only
            if target_currency.upper() == "USDT" and status == "active":
                base_clean = base_currency.upper()
                if base_clean in equity_blacklist:
                    continue

                symbol_name = f"{base_clean}/USDT:USDT"
                try:
                    volume = float(item.get("volume_24_hours", 0) or 0)
                except (ValueError, TypeError):
                    volume = 0.0
                
                usdt_pairs.append({
                    "symbol": symbol_name,
                    "volume": volume
                })

        # Sort descending by 24h volume
        usdt_pairs.sort(key=lambda x: x["volume"], reverse=True)
        top_symbols = [x["symbol"] for x in usdt_pairs[:limit]]

        logger.info(f"Successfully retrieved and ranked {len(top_symbols)} top USDT pairs from CoinDCX.")
        return top_symbols

    except Exception as e:
        logger.error(f"Error querying CoinDCX markets: {e}. Defaulting to CCXT native Binance loader.")
        return []

def get_active_futures_symbols():
    """
    Primary symbol loader: Validates that all candidate pairs actually exist on Binance USDⓈ-M Futures.
    """
    try:
        logger.info("Loading valid perpetual markets directly from Binance USDⓈ-M exchange...")
        markets = exchange.load_markets()
        valid_binance_symbols = {
            s for s, m in markets.items()
            if m.get('active', True)
            and m.get('quote') == 'USDT'
            and (m.get('linear', False) or m.get('swap', False) or m.get('contract', False))
        }

        # Candidate pool from CoinDCX
        coindcx_candidates = fetch_coindcx_top_pairs(limit=160)
        filtered_symbols = [s for s in coindcx_candidates if s in valid_binance_symbols]

        if len(filtered_symbols) >= 30:
            return filtered_symbols[:120]

        # Fallback to pure Binance volume sorting
        sorted_binance = sorted(list(valid_binance_symbols))[:120]
        logger.info(f"Loaded {len(sorted_binance)} confirmed USDⓈ-M futures pairs from Binance.")
        return sorted_binance

    except Exception as e:
        logger.error(f"Error loading Binance futures markets: {e}")
        return [
            "BTC/USDT:USDT",
            "ETH/USDT:USDT",
            "SOL/USDT:USDT",
            "BNB/USDT:USDT",
            "XRP/USDT:USDT",
            "DOGE/USDT:USDT",
            "ADA/USDT:USDT",
            "AVAX/USDT:USDT",
            "NEAR/USDT:USDT",
            "SUI/USDT:USDT",
            "ENA/USDT:USDT",
            "LINK/USDT:USDT"
        ]

# =====================================================================
# 4. 15-MINUTE MARKET STRUCTURE SHIFT (MSS) ENGINE
# =====================================================================
def extract_15m_levels(symbol, setup_type, h_ref, l_ref, trig_timestamp):
    """
    Extracts the highest swing high or lowest swing low inside the exact 4-hour window
    of the trigger candle so the entry level remains static and reliable.
    """
    try:
        # Fetch 15m candles starting from the trigger candle open time (4 hours = 16 x 15m bars)
        # Fetch up to 32 bars to encompass both trigger bar and follow-up bars
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', since=trig_timestamp, limit=20)
        if len(ohlcv_15m) < 16:
            # Fallback to fetching recent candles if since parameter is constrained
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=24)
            if len(ohlcv_15m) < 16:
                logger.warning(f"[{symbol}] Insufficient 15m candle history (count: {len(ohlcv_15m)})")
                return None

        # Lock exact 16 bars corresponding to that 4H trigger session
        window_15m = ohlcv_15m[:16]

        if setup_type == "LONG":
            # 1. Absolute lowest point during that 4H sweep session
            l_min = min(c[3] for c in window_15m)
            min_idx = [i for i, c in enumerate(window_15m) if c[3] == l_min][0]

            # 2. Highest swing point prior to or at the sweep low
            h_mss = None
            for i in range(min_idx, -1, -1):
                c = window_15m[i]
                if h_mss is None or c[2] > h_mss:
                    h_mss = c[2]

            if h_mss is None:
                logger.info(f"[{symbol}] LONG MSS Check: No valid swing high found.")
                return None

            # 3. Structural Boundary: 15m entry high must sit strictly above reference 4H low
            if h_mss <= l_ref:
                logger.info(f"[{symbol}] LONG MSS Filtered: H_mss ({h_mss}) <= Reference Low ({l_ref})")
                return None

            # Genuine Opposing Liquidity Target & Mathematical R:R
            target = h_ref
            risk = h_mss - l_min
            reward = target - h_mss

            if risk <= 0 or reward <= 0:
                logger.info(
                    f"[{symbol}] LONG MSS Filtered: Invalid Risk/Reward "
                    f"(Risk: {risk}, Reward: {reward})"
                )
                return None

            rr_ratio = round(reward / risk, 2)
            logger.info(
                f"[{symbol}] LONG MSS Locked | Entry: {h_mss} | "
                f"SL: {l_min} | Target: {target} | R:R: 1:{rr_ratio}"
            )

            return {
                "l_min": l_min,
                "h_mss": h_mss,
                "l_ref": l_ref,
                "h_ref": h_ref,
                "entry": h_mss,
                "stop_loss": l_min,
                "target": target,
                "rr_ratio": rr_ratio
            }

        elif setup_type == "SHORT":
            # 1. Absolute highest point during that 4H sweep session
            h_max = max(c[2] for c in window_15m)
            max_idx = [i for i, c in enumerate(window_15m) if c[2] == h_max][0]

            # 2. Lowest swing point prior to or at the sweep high
            l_mss = None
            for i in range(max_idx, -1, -1):
                c = window_15m[i]
                if l_mss is None or c[3] < l_mss:
                    l_mss = c[3]

            if l_mss is None:
                logger.info(f"[{symbol}] SHORT MSS Check: No valid swing low found.")
                return None

            # 3. Structural Boundary: 15m entry low must sit strictly below reference 4H high
            if l_mss >= h_ref:
                logger.info(f"[{symbol}] SHORT MSS Filtered: L_mss ({l_mss}) >= Reference High ({h_ref})")
                return None

            # Genuine Opposing Liquidity Target & Mathematical R:R
            target = l_ref
            risk = h_max - l_mss
            reward = l_mss - target

            if risk <= 0 or reward <= 0:
                logger.info(
                    f"[{symbol}] SHORT MSS Filtered: Invalid Risk/Reward "
                    f"(Risk: {risk}, Reward: {reward})"
                )
                return None

            rr_ratio = round(reward / risk, 2)
            logger.info(
                f"[{symbol}] SHORT MSS Locked | Entry: {l_mss} | "
                f"SL: {h_max} | Target: {target} | R:R: 1:{rr_ratio}"
            )

            return {
                "h_max": h_max,
                "l_mss": l_mss,
                "h_ref": h_ref,
                "l_ref": l_ref,
                "entry": l_mss,
                "stop_loss": h_max,
                "target": target,
                "rr_ratio": rr_ratio
            }

    except Exception as e:
        logger.error(f"[{symbol}] Error in extract_15m_levels: {e}")
        return None

# =====================================================================
# 5. 4H LIQUIDITY SWEEP EVALUATOR
# =====================================================================
def check_liquidity_sweep(symbol):
    """
    Evaluates closed 4H candles to detect valid bullish/bearish liquidity sweeps.
    """
    try:
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=5)
        if len(ohlcv_4h) < 4:
            logger.warning(f"[{symbol}] Insufficient 4H candle history.")
            return None

        # Candle Indices:
        # [-3] = Reference Candle
        # [-2] = Trigger Candle (Just closed)
        # [-1] = Current forming candle
        c_ref = ohlcv_4h[-3]
        c_trig = ohlcv_4h[-2]

        h_ref, l_ref = c_ref[2], c_ref[3]
        h_trig, l_trig, c_trig_close = c_trig[2], c_trig[3], c_trig[4]
        trig_timestamp = c_trig[0]

        # 1. LONG SWEEP: Low swept, Closed inside reference body, did not sweep opposite high
        long_sweep = (l_trig < l_ref) and (c_trig_close > l_ref) and (h_trig <= h_ref)

        # 2. SHORT SWEEP: High swept, Closed inside reference body, did not sweep opposite low
        short_sweep = (h_trig > h_ref) and (c_trig_close < h_ref) and (l_trig >= l_ref)

        if not (long_sweep or short_sweep):
            return None

        setup_type = "LONG" if long_sweep else "SHORT"
        logger.info(
            f"[{symbol}] 4H {setup_type} Sweep Identified | "
            f"Ref: [{l_ref}, {h_ref}] | Trig: [{l_trig}, {h_trig}, {c_trig_close}]"
        )

        # Extract 15m MSS structural levels tied to the exact 4H trigger session
        levels = extract_15m_levels(symbol, setup_type, h_ref, l_ref, trig_timestamp)
        if not levels:
            logger.info(f"[{symbol}] 4H Sweep disqualified during 15m structural extraction.")
            return None

        # Check if 15m MSS break already occurred on recent closed candles
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
        logger.error(f"[{symbol}] Liquidity Sweep Check Exception: {e}")
        return None

# =====================================================================
# 6. MARKET SCAN WORKERS & ALERT DISPATCHERS
# =====================================================================
def run_full_scan():
    """
    Executes full market scan over verified Binance Futures pairs and prevents repeat alerts for the same candle.
    """
    scan_start = time.time()
    logger.info("=== Starting Full 4H Liquidity Sweep Scan ===")
    try:
        symbols = get_active_futures_symbols()
        found_setups = []

        for sym in symbols:
            res = check_liquidity_sweep(sym)
            if res:
                sweep_id = (sym, res["candle_timestamp"])
                
                # Check if this exact 4H sweep setup has already been sent to avoid duplicate alert loops
                if sweep_id not in processed_sweeps:
                    found_setups.append(res)
                    processed_sweeps.add(sweep_id)
                
                # Maintain active watchlist for ongoing 15m monitoring
                active_watchlists[sym] = res
            time.sleep(0.04)

        elapsed = round(time.time() - scan_start, 1)

        # Build Full Structured Telegram Alert
        msg = "⚡ *4H LIQUIDITY SWEEP SCAN COMPLETE* ⚡\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📊 *Pairs Evaluated:* `{len(symbols)}`\n"
        msg += f"🎯 *Setups Identified:* `{len(found_setups)}`\n"
        msg += f"⏱ *Scan Duration:* `{elapsed}s`\n"
        msg += f"⏰ *Time (UTC):* `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}`\n"

        if found_setups:
            for s in found_setups:
                sym_clean = s['symbol'].split(':')[0]
                lvl = s['levels']
                status_icon = "🟢" if s['type'] == "LONG" else "🔴"
                msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
                msg += f"{status_icon} *{sym_clean}* | *{s['type']} SWEEP*\n"
                msg += f"• *Entry Level (15m MSS):* `{lvl['entry']}`\n"
                msg += f"• *Invalidation (SL):* `{lvl['stop_loss']}`\n"
                msg += f"• *Target (4H Liquidity):* `{lvl['target']}`\n"
                msg += f"• *Risk : Reward:* `1 : {lvl['rr_ratio']}`\n"
                msg += f"• *MSS Triggered:* `{'YES (Immediate)' if s['already_mss'] else 'NO (Monitoring 15m)'}`"
        else:
            msg += "\nNo fresh 4H sweeps currently meeting all structural parameters."

        send_telegram_alert(msg)
        
        # Log telemetry record
        scan_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pairs_evaluated": len(symbols),
            "setups_found": len(found_setups),
            "duration_seconds": elapsed
        })
        if len(scan_history) > 20:
            scan_history.pop(0)

        logger.info(f"4H Scan completed in {elapsed}s with {len(found_setups)} new setups dispatched.")

    except Exception as e:
        logger.error(f"Scan Execution Exception: {e}")

def run_15m_check():
    """
    Monitors active watchlist setups on 15m closes and eliminates them upon breakout or invalidation.
    """
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
                # Invalidation Check: Price closed/pierced below sweep low
                if last_closed[3] < levels['l_min']:
                    logger.info(f"[{sym}] LONG Setup Invalidated (Broke sweep low).")
                    to_remove.append(sym)
                    continue

                # MSS Break Confirmation: Closed above swing high
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
                # Invalidation Check: Price closed/pierced above sweep high
                if last_closed[2] > levels['h_max']:
                    logger.info(f"[{sym}] SHORT Setup Invalidated (Broke sweep high).")
                    to_remove.append(sym)
                    continue

                # MSS Break Confirmation: Closed below swing low
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
    """
    Perpetual loop that triggers 4H full scans and 15m checks on candle close.
    """
    logger.info("Background scheduler thread active.")
    while True:
        now = datetime.now(timezone.utc)
        # Execute 1 minute past every 15-minute close (:01, :16, :31, :46)
        if now.minute in [1, 16, 31, 46] and now.second < 20:
            # 4H Binance Candle Close Windows (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC)
            if now.hour in [0, 4, 8, 12, 16, 20] and now.minute == 1:
                run_full_scan()
            else:
                run_15m_check()
            time.sleep(60)
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
    send_telegram_alert("⚡ *Manual scan initiated via Webhook...* Evaluating top pairs now.")
    threading.Thread(target=run_full_scan, daemon=True).start()
    return "Manual 4H Sweep + 15m MSS scan started! Check Telegram in 2 minutes."

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
    # Dispatch Startup Alert to Telegram
    send_telegram_alert(
        "🚀 *Crypto Trading Bot is Online & Active on Render!* \n\n"
        "• *Engine:* 4H Sweep + 15m MSS (Epoch Locked)\n"
        "• *Target System:* Dynamic 4H Opposing Liquidity\n"
        "• *Coverage:* Top Volume USDⓈ-M Futures Perpetuals\n"
        "• *Status:* 24/7 Monitoring Initialized"
    )

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    logger.info("=== 24/7 Market Monitor Initialized Successfully ===")
    app.run(host='0.0.0.0', port=PORT)
