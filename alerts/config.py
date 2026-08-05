"""
Configuration for CoinDCX 4H Fakeout Alert System.

WATCH_PAIRS is loaded dynamically from CoinDCX's active futures instruments
endpoint on startup. If the fetch fails, it falls back to FALLBACK_PAIRS so
the bot can still run.
"""

from coindcx import get_active_futures_pairs

# ---------------------------------------------------------------------------
# Fallback list used when the API endpoint is unreachable at startup
# ---------------------------------------------------------------------------
FALLBACK_PAIRS = [
    "B-BTC_USDT",
    "B-ETH_USDT",
    "B-SOL_USDT",
    "B-BNB_USDT",
    "B-XRP_USDT",
]

# ---------------------------------------------------------------------------
# Dynamically fetched on import — all active CoinDCX USDT futures pairs
# ---------------------------------------------------------------------------
WATCH_PAIRS: list[str] = get_active_futures_pairs() or FALLBACK_PAIRS

# ---------------------------------------------------------------------------
# How often to check for new 4H candle closes (seconds).
# 4H candles close every 4 hours; checking every 5 minutes is plenty.
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Alert cooldown: minimum seconds between repeated alerts for the same
# pair + direction. Candle-close alerts fire at most once per close, so
# this mostly guards against races on restarts.
# ---------------------------------------------------------------------------
ALERT_COOLDOWN_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Volume filter: skip pairs whose 24-hour rolling USDT volume (base volume
# × last price) is below this threshold before running sweep checks.
# Using rolling volume avoids false-quiet readings at UTC midnight resets.
# ---------------------------------------------------------------------------
MIN_24H_VOLUME_USDT = 5_000_000  # $5 M USDT
