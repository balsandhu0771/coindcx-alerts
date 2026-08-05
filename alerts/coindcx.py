"""
CoinDCX public API client.
Uses only the public REST endpoints — no API key required.

Notes on the API:
  - Candle endpoint supports intervals: 1m, 15m, 1h, 1d  (no native 4H)
    → we fetch 1H candles and aggregate them into UTC-aligned 4H periods.
  - Ticker "market" field uses the coindcx_name format (e.g. "BTCUSDT"),
    while the candle "pair" parameter uses the pair format ("B-BTC_USDT").
  - Candle entries are returned newest-first.
  - Only buckets with all 4 completed 1H bars are considered a closed 4H candle.
    The still-forming bucket is never included in the output of get_closed_4h_candles().
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BASE_URL        = "https://public.coindcx.com"
COINDCX_API_URL = "https://api.coindcx.com"
TICKER_URL      = f"{BASE_URL}/exchange/ticker"
CANDLES_URL     = f"{BASE_URL}/market_data/candles"

_session = requests.Session()
_session.headers.update({"User-Agent": "CoinDCX-Alert-Bot/1.0"})


# ---------------------------------------------------------------------------
# Active instruments
# ---------------------------------------------------------------------------

def get_active_futures_pairs() -> list[str]:
    """
    Fetch all active USDT futures pair names from CoinDCX.
    Endpoint returns a plain list of strings like ['B-BTC_USDT', ...].
    Falls back to an empty list on error so callers can use their own fallback.
    """
    url = f"{COINDCX_API_URL}/exchange/v1/derivatives/futures/data/active_instruments"
    try:
        response = _session.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            logger.warning("Unexpected futures instruments response shape: %s", type(data))
            return []
        pairs = [p for p in data if isinstance(p, str) and p.endswith("_USDT")]
        logger.info("Loaded %d active USDT futures pairs from CoinDCX", len(pairs))
        return pairs
    except requests.RequestException as exc:
        logger.error("Failed to fetch active futures pairs: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Ticker (live prices — used for dashboard display only, not for alerts)
# ---------------------------------------------------------------------------

def _pair_to_market(pair: str) -> str:
    """Convert candle-style pair to ticker market name.  "B-BTC_USDT" → "BTCUSDT" """
    return pair.replace("B-", "").replace("_", "")


def _fetch_ticker_raw() -> list[dict]:
    """Fetch the full ticker snapshot once; shared by price and volume helpers."""
    try:
        response = _session.get(TICKER_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.error("Ticker fetch failed: %s", exc)
        return []


def get_ticker_prices(pairs: list[str]) -> dict[str, float]:
    """
    Fetch the latest last-traded price for each requested pair.
    Returns a dict: { "B-BTC_USDT": 67500.0, ... }
    Missing pairs are silently omitted.
    """
    data = _fetch_ticker_raw()

    market_to_price: dict[str, float] = {}
    for entry in data:
        market = entry.get("market", "")
        try:
            market_to_price[market] = float(entry["last_price"])
        except (KeyError, ValueError, TypeError):
            pass

    prices: dict[str, float] = {}
    for pair in pairs:
        market = _pair_to_market(pair)
        if market in market_to_price:
            prices[pair] = market_to_price[market]
    return prices


def get_24h_volumes(pairs: list[str]) -> dict[str, float]:
    """
    Return 24-hour rolling USDT volume for each requested pair.

    CoinDCX's ticker 'volume' field is the base-currency volume traded in the
    rolling 24-hour window.  Multiplying by 'last_price' converts it to USDT.
    This is a rolling window (not a calendar-day reset), so it remains accurate
    at any hour of the day.

    Returns a dict: { "B-BTC_USDT": 1_234_567.0, ... }
    Pairs with missing or unparseable data are omitted.
    """
    data = _fetch_ticker_raw()

    market_to_vol_usdt: dict[str, float] = {}
    for entry in data:
        market = entry.get("market", "")
        try:
            price  = float(entry["last_price"])
            volume = float(entry["volume"])
            market_to_vol_usdt[market] = volume * price
        except (KeyError, ValueError, TypeError):
            pass

    result: dict[str, float] = {}
    for pair in pairs:
        market = _pair_to_market(pair)
        if market in market_to_vol_usdt:
            result[pair] = market_to_vol_usdt[market]
    return result


# ---------------------------------------------------------------------------
# Closed 4H candles (core data source for alert detection)
# ---------------------------------------------------------------------------

def _aggregate_1h_to_4h(candles_1h: list[dict]) -> list[dict]:
    """
    Group 1H candles into UTC-aligned 4H buckets (00–03, 04–07, 08–11, …).

    Rules:
      - A bucket must contain exactly 4 completed 1H bars to be included.
        Buckets with fewer bars are still forming and are excluded entirely.
      - OHLCV: open = first bar open, high = max highs, low = min lows,
               close = last bar close, volume = sum of volumes.

    Returns completed 4H candles sorted newest-first.
    """
    buckets: dict[int, list[dict]] = defaultdict(list)
    for c in candles_1h:
        ts_sec = c["time"] // 1000                          # ms → s
        dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        bucket_hour = (dt.hour // 4) * 4                   # round down to 4H boundary
        bucket_ts = int(
            datetime(dt.year, dt.month, dt.day, bucket_hour, tzinfo=timezone.utc).timestamp()
        ) * 1000
        buckets[bucket_ts].append(c)

    # Keep only complete buckets (all 4 hourly bars present)
    complete = {ts: cs for ts, cs in buckets.items() if len(cs) >= 4}

    result = []
    for ts in sorted(complete.keys(), reverse=True):       # newest first
        cs = sorted(complete[ts], key=lambda x: x["time"])
        result.append({
            "time":   ts,
            "open":   cs[0]["open"],
            "high":   max(c["high"]   for c in cs),
            "low":    min(c["low"]    for c in cs),
            "close":  cs[-1]["close"],
            "volume": sum(c["volume"] for c in cs),
        })

    return result


def get_closed_4h_candles(pair: str, count: int = 3) -> list[dict]:
    """
    Return the `count` most recently *closed* 4H candles for a pair,
    sorted newest-first.

    Caller indexing matches the user's ohlcv notation:
      result[0]  →  ohlcv[-2]  — the candle that just closed
      result[1]  →  ohlcv[-3]  — the previous closed candle (reference)

    ohlcv[-1] (the still-forming candle) is NEVER returned because any
    bucket with fewer than 4 completed 1H bars is excluded by _aggregate_1h_to_4h().

    Returns an empty list on API error or if fewer than 2 complete candles
    are available.
    """
    # Worst-case: the forming bucket holds 3 of 4 bars, so we need count*4 + 3
    # extra bars.  We round up generously and cap at 50.
    limit = min(50, count * 4 + 8)

    try:
        response = _session.get(
            CANDLES_URL,
            params={"pair": pair, "interval": "1h", "limit": limit},
            timeout=10,
        )
        response.raise_for_status()
        raw = response.json()
    except requests.RequestException as exc:
        logger.error("Candles fetch failed for %s: %s", pair, exc)
        return []

    if not raw or len(raw) < 8:
        logger.debug("Not enough 1H bars for %s (got %d)", pair, len(raw) if raw else 0)
        return []

    candles_1h: list[dict] = []
    for entry in raw:
        try:
            candles_1h.append({
                "time":   int(entry["time"]),
                "open":   float(entry["open"]),
                "high":   float(entry["high"]),
                "low":    float(entry["low"]),
                "close":  float(entry["close"]),
                "volume": float(entry["volume"]),
            })
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Skipping malformed candle entry for %s: %s", pair, exc)

    candles_4h = _aggregate_1h_to_4h(candles_1h)

    if len(candles_4h) < 2:
        logger.debug(
            "Insufficient complete 4H candles for %s (got %d from %d 1H bars)",
            pair, len(candles_4h), len(candles_1h),
        )
        return candles_4h  # return what we have; caller checks length

    return candles_4h[:count]
