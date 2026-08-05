#!/usr/bin/env python3
"""
CoinDCX 4H Fakeout Alert System — Flask Web Dashboard
======================================================
Monitors closed 4H candles for fakeout setups and sends Telegram alerts.
Alerts fire ONLY on confirmed closed candles:
  ohlcv[-2]  =  the candle that just closed       (test candle)
  ohlcv[-3]  =  the previous closed candle         (reference)
  ohlcv[-1]  =  still-forming candle — never used

Run with:
    python3 main.py
"""

import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, jsonify, render_template_string

import config
from coindcx import get_24h_volumes, get_closed_4h_candles, get_ticker_prices
from detector import CandleCloseDetector
from telegram_bot import send_alert, test_connection

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Shared state  (written by monitor thread, read by Flask routes)
# ---------------------------------------------------------------------------
_lock          = threading.Lock()
_pair_status:  dict[str, dict] = {}          # pair → candle/price snapshot
_alert_history: deque          = deque(maxlen=100)
_monitor_running               = False
_poll_count                    = 0

# ---------------------------------------------------------------------------
# Background monitoring loop
# ---------------------------------------------------------------------------

def _monitor_loop() -> None:
    global _monitor_running, _poll_count

    detector = CandleCloseDetector(cooldown_seconds=config.ALERT_COOLDOWN_SECONDS)
    _monitor_running = True

    logger.info(
        "Candle-close monitor started — %d pairs, checking every %ds",
        len(config.WATCH_PAIRS), config.POLL_INTERVAL_SECONDS,
    )
    logger.info(
        "Detection: ohlcv[-2] vs ohlcv[-3] — alerts only on fully closed candles"
    )

    while _monitor_running:
        _poll_count += 1
        logger.info(
            "[poll #%d] Scanning %d pairs for new 4H candle closes…",
            _poll_count, len(config.WATCH_PAIRS),
        )

        # Fetch live prices and 24h volumes in one bulk call each.
        # Prices are for dashboard display only; volumes gate which pairs
        # proceed to the (more expensive) per-pair candle fetch.
        prices  = get_ticker_prices(config.WATCH_PAIRS)
        volumes = get_24h_volumes(config.WATCH_PAIRS)

        min_vol = config.MIN_24H_VOLUME_USDT
        eligible = [
            p for p in config.WATCH_PAIRS
            if volumes.get(p, 0) >= min_vol
        ]
        skipped = len(config.WATCH_PAIRS) - len(eligible)
        logger.info(
            "[poll #%d] Volume filter ($%.0fM): %d/%d pairs qualify (%d skipped)",
            _poll_count, min_vol / 1_000_000,
            len(eligible), len(config.WATCH_PAIRS), skipped,
        )

        alerts_this_poll = 0
        for pair in eligible:
            # ── Fetch the last 3 closed 4H candles ──────────────────────────
            # result[0] = ohlcv[-2]  (just closed — test candle)
            # result[1] = ohlcv[-3]  (reference candle)
            # ohlcv[-1] (forming) is excluded by get_closed_4h_candles()
            candles = get_closed_4h_candles(pair, count=3)
            if len(candles) < 2:
                continue

            just_closed = candles[0]   # ohlcv[-2]
            reference   = candles[1]   # ohlcv[-3]

            # Update dashboard status for this pair
            candle_dt = datetime.fromtimestamp(
                just_closed["time"] / 1000, tz=timezone.utc
            )
            with _lock:
                _pair_status.setdefault(pair, {}).update({
                    "live_price":   prices.get(pair),
                    "jc_open":      just_closed["open"],
                    "jc_high":      just_closed["high"],
                    "jc_low":       just_closed["low"],
                    "jc_close":     just_closed["close"],
                    "ref_high":     reference["high"],
                    "ref_low":      reference["low"],
                    "candle_time":  candle_dt.strftime("%m-%d %H:%M UTC"),
                    "updated":      datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                })

            # ── Evaluate the closed candle for fakeout ───────────────────────
            events = detector.process_candles(pair, candles)
            for event in events:
                alerts_this_poll += 1
                symbol = pair.replace("B-", "").replace("_", "/")
                logger.info(
                    "ALERT ▶ %-15s  %-15s  close=%.4f  swept=%.4f",
                    symbol, event.alert_type, event.close_price, event.swept_level,
                )
                send_alert(event.format_message())
                with _lock:
                    _alert_history.appendleft({
                        "time":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "pair":   symbol,
                        "type":   event.alert_type,
                        "close":  event.close_price,
                        "swept":  event.swept_level,
                        "ref_high": event.ref_high,
                        "ref_low":  event.ref_low,
                    })

        logger.info(
            "[poll #%d] Done — %d alert(s) fired. Next check in %ds.",
            _poll_count, alerts_this_poll, config.POLL_INTERVAL_SECONDS,
        )
        time.sleep(config.POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CoinDCX 4H Fakeout Alerts</title>
  <meta http-equiv="refresh" content="60">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; padding: 24px; }
    h1  { font-size: 1.5rem; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }
    .subtitle { color: #64748b; font-size: 0.82rem; margin-bottom: 6px; }
    .note { color: #475569; font-size: 0.75rem; margin-bottom: 28px; font-style: italic; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; margin-bottom: 36px; }
    .card { background: #1e2130; border: 1px solid #2d3148; border-radius: 12px; padding: 16px; }
    .card .symbol { font-size: 0.9rem; font-weight: 700; color: #94a3b8; margin-bottom: 8px; letter-spacing: .04em; }
    .card .live  { font-size: 1.3rem; font-weight: 700; color: #f1f5f9; margin-bottom: 10px; }
    .card .ohlcv { font-size: 0.73rem; color: #64748b; line-height: 1.85; }
    .card .ohlcv .lbl { display: inline-block; width: 120px; color: #475569; }
    .card .ohlcv .val { color: #94a3b8; font-variant-numeric: tabular-nums; }
    .card .ohlcv .hi  { color: #4ade80; }
    .card .ohlcv .lo  { color: #f87171; }
    .divider { border-top: 1px solid #2d3148; margin: 8px 0; }
    .ts { font-size: 0.68rem; color: #334155; margin-top: 8px; }
    h2  { font-size: 1.05rem; font-weight: 600; color: #f1f5f9; margin-bottom: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th  { text-align: left; color: #64748b; font-weight: 500; padding: 6px 12px; border-bottom: 1px solid #2d3148; }
    td  { padding: 9px 12px; border-bottom: 1px solid #1a1f2e; color: #cbd5e1; font-variant-numeric: tabular-nums; }
    tr:last-child td { border-bottom: none; }
    .t-FH { color: #f87171; font-weight: 700; }
    .t-FL { color: #4ade80; font-weight: 700; }
    .empty { color: #475569; font-style: italic; padding: 20px 12px; }
    .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#4ade80; margin-right:6px; animation:pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  </style>
</head>
<body>
  <h1><span class="dot"></span>CoinDCX 4H Fakeout Alerts</h1>
  <p class="subtitle">Monitoring {{ pair_count }} pairs · poll #{{ poll_count }} · page refreshes every 60s</p>
  <p class="note">Alerts fire only on closed candles: ohlcv[−2] sweeps ohlcv[−3]'s High/Low and closes back inside the range.</p>

  <div class="grid">
  {% for pair, s in pairs.items() %}
  {% set sym = pair.replace('B-','').replace('_','/') %}
  <div class="card">
    <div class="symbol">{{ sym }}</div>
    <div class="live">{{ "%.4f"|format(s.live_price) if s.live_price else "—" }}
      <span style="font-size:.7rem;color:#475569;font-weight:400;"> live</span>
    </div>
    <div class="ohlcv">
      <span style="font-size:.68rem;color:#334155;">── ohlcv[−2] closed candle ──</span><br>
      <span class="lbl">Close:</span><span class="val">{{ "%.4f"|format(s.jc_close) if s.jc_close else "—" }}</span><br>
      <span class="lbl">High:</span><span class="val hi">{{ "%.4f"|format(s.jc_high) if s.jc_high else "—" }}</span><br>
      <span class="lbl">Low:</span><span class="val lo">{{ "%.4f"|format(s.jc_low) if s.jc_low else "—" }}</span>
      <div class="divider"></div>
      <span style="font-size:.68rem;color:#334155;">── ohlcv[−3] reference candle ──</span><br>
      <span class="lbl">Ref High:</span><span class="val">{{ "%.4f"|format(s.ref_high) if s.ref_high else "—" }}</span><br>
      <span class="lbl">Ref Low:</span><span class="val">{{ "%.4f"|format(s.ref_low) if s.ref_low else "—" }}</span>
    </div>
    {% if s.candle_time %}<div class="ts">Candle: {{ s.candle_time }} · checked {{ s.updated }}</div>{% endif %}
  </div>
  {% endfor %}
  </div>

  <h2>Recent Alerts</h2>
  {% if alerts %}
  <table>
    <thead>
      <tr><th>Time</th><th>Pair</th><th>Alert</th><th>Close (ohlcv[−2])</th><th>Swept Level</th><th>Ref High</th><th>Ref Low</th></tr>
    </thead>
    <tbody>
    {% for a in alerts %}
    <tr>
      <td style="color:#475569;white-space:nowrap">{{ a.time }}</td>
      <td style="font-weight:600">{{ a.pair }}</td>
      <td class="{{ 't-FH' if a.type == 'FAKEOUT_HIGH' else 't-FL' }}">
        {{ 'Bearish Fakeout ↑ rejected' if a.type == 'FAKEOUT_HIGH' else 'Bullish Fakeout ↓ rejected' }}
      </td>
      <td>{{ "%.4f"|format(a.close) }}</td>
      <td>{{ "%.4f"|format(a.swept) }}</td>
      <td style="color:#4ade80">{{ "%.4f"|format(a.ref_high) }}</td>
      <td style="color:#f87171">{{ "%.4f"|format(a.ref_low) }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No fakeout alerts yet — watching for closed candle setups.</div>
  {% endif %}
</body>
</html>
"""


@app.route("/")
def dashboard():
    with _lock:
        pairs  = dict(_pair_status)
        alerts = list(_alert_history)
    return render_template_string(
        DASHBOARD_HTML,
        pairs=pairs,
        alerts=alerts,
        pair_count=len(config.WATCH_PAIRS),
        poll_count=_poll_count,
    )


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify({
            "running":       _monitor_running,
            "poll_count":    _poll_count,
            "pairs":         dict(_pair_status),
            "recent_alerts": list(_alert_history)[:20],
        })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=" * 60)
    logger.info("  CoinDCX 4H Fakeout Alert System — Candle-Close Mode")
    logger.info("  Detection: ohlcv[-2] vs ohlcv[-3] (closed candles only)")
    logger.info("=" * 60)

    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
    if missing:
        logger.warning("Missing secrets: %s — Telegram alerts will be skipped", missing)
    else:
        logger.info("Testing Telegram connection…")
        if test_connection():
            logger.info("Telegram OK ✓")
        else:
            logger.warning("Telegram connection failed — check your bot token and chat ID")

    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting Flask dashboard on port %d…", port)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
