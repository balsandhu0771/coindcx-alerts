"""
Candle-close fakeout detector.

Detection rule (evaluated ONLY on fully closed candles):

  ohlcv[-1]  =  still-forming candle  →  NEVER evaluated
  ohlcv[-2]  =  most recently CLOSED 4H candle  (test candle)
  ohlcv[-3]  =  the candle before that           (reference candle)

An alert fires when ohlcv[-2] swept ohlcv[-3]'s High or Low AND its
Close is back inside ohlcv[-3]'s High-Low range:

  FAKEOUT_HIGH  →  ohlcv[-2].high  > ohlcv[-3].high
                   AND ohlcv[-3].low <= ohlcv[-2].close <= ohlcv[-3].high

  FAKEOUT_LOW   →  ohlcv[-2].low   < ohlcv[-3].low
                   AND ohlcv[-3].low <= ohlcv[-2].close <= ohlcv[-3].high

Each candle close is evaluated exactly once per pair (tracked by timestamp),
so there is no duplicated alert risk between polls.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AlertEvent:
    pair:             str
    alert_type:       str    # "FAKEOUT_HIGH" | "FAKEOUT_LOW"
    close_price:      float  # ohlcv[-2].close
    swept_level:      float  # ohlcv[-3].high or ohlcv[-3].low that was exceeded
    ref_high:         float  # ohlcv[-3].high
    ref_low:          float  # ohlcv[-3].low
    jc_high:          float  # ohlcv[-2].high  (the wick that swept the level)
    jc_low:           float  # ohlcv[-2].low   (the wick that swept the level)
    candle_time_ms:   int    # ohlcv[-2] bucket open timestamp (ms UTC)

    def format_message(self) -> str:
        from datetime import datetime, timezone
        symbol = self.pair.replace("B-", "").replace("_", "/")
        candle_dt = datetime.fromtimestamp(self.candle_time_ms / 1000, tz=timezone.utc)
        candle_str = candle_dt.strftime("%Y-%m-%d %H:%M UTC")

        if self.alert_type == "FAKEOUT_HIGH":
            icon  = "⚠️🔻"
            title = "Bearish Fakeout — 4H High Swept & Rejected"
            desc  = (
                "The closed 4H candle *swept above* the previous candle's high "
                "but *closed back inside* the range — confirmed bearish fakeout.\n"
                "Consider short setups on the next candle open."
            )
            wick_label = "ohlcv\\[\\-2\\] High (wick)"
            wick_val   = self.jc_high
        else:
            icon  = "⚠️🔺"
            title = "Bullish Fakeout — 4H Low Swept & Rejected"
            desc  = (
                "The closed 4H candle *swept below* the previous candle's low "
                "but *closed back inside* the range — confirmed bullish fakeout.\n"
                "Consider long setups on the next candle open."
            )
            wick_label = "ohlcv\\[\\-2\\] Low (wick)"
            wick_val   = self.jc_low

        return (
            f"{icon} *{symbol} — {title}*\n\n"
            f"{desc}\n\n"
            f"🕯 *Candle closed at:* `{candle_str}`\n"
            f"📍 *Close (ohlcv\\[\\-2\\]):*  `{self.close_price:,.4f}`\n"
            f"📐 *{wick_label}:*  `{wick_val:,.4f}`\n"
            f"🎯 *Swept level (ohlcv\\[\\-3\\]):*  `{self.swept_level:,.4f}`\n"
            f"🔺 *Ref High (ohlcv\\[\\-3\\]):*  `{self.ref_high:,.4f}`\n"
            f"🔻 *Ref Low  (ohlcv\\[\\-3\\]):*  `{self.ref_low:,.4f}`"
        )


class CandleCloseDetector:
    """
    Evaluates completed 4H candles for fakeout setups.

    Call process_candles(pair, candles) after every candle fetch.
    The detector tracks which candle close it last evaluated per pair and
    skips duplicate evaluations, so calling it on every poll is safe.
    """

    def __init__(self, cooldown_seconds: int = 300):
        # pair → timestamp (ms) of the last ohlcv[-2] we already evaluated
        self._last_seen:       dict[str, int]        = {}
        # "pair:type" → unix timestamp of last fired alert (for cooldown)
        self._last_alert_time: dict[str, float]      = {}
        self._cooldown = cooldown_seconds

    def process_candles(self, pair: str, candles: list[dict]) -> list[AlertEvent]:
        """
        Evaluate the latest batch of closed 4H candles for a pair.

        Parameters
        ----------
        pair    : e.g. "B-BTC_USDT"
        candles : newest-first list of completed 4H candles, as returned by
                  get_closed_4h_candles().
                    candles[0]  →  ohlcv[-2]  (just closed — the test candle)
                    candles[1]  →  ohlcv[-3]  (previous closed — the reference)

        Returns a list of AlertEvents (typically 0 or 1; at most 2 if both
        high and low were simultaneously swept).
        """
        if len(candles) < 2:
            return []

        just_closed = candles[0]   # ohlcv[-2]
        reference   = candles[1]   # ohlcv[-3]

        # ── Skip if we've already evaluated this exact candle close ──────────
        candle_time = just_closed["time"]
        if self._last_seen.get(pair) == candle_time:
            return []
        self._last_seen[pair] = candle_time

        ref_high = reference["high"]
        ref_low  = reference["low"]
        jc_high  = just_closed["high"]
        jc_low   = just_closed["low"]
        jc_close = just_closed["close"]

        events: list[AlertEvent] = []

        # ── FAKEOUT_HIGH ─────────────────────────────────────────────────────
        # Condition: ohlcv[-2].high > ohlcv[-3].high  (wick swept above)
        #        AND ohlcv[-3].low <= ohlcv[-2].close <= ohlcv[-3].high  (close inside)
        if jc_high > ref_high and ref_low <= jc_close <= ref_high:
            logger.info(
                "FAKEOUT_HIGH %s | jc.high=%.4f > ref.high=%.4f | jc.close=%.4f ∈ [%.4f, %.4f]",
                pair, jc_high, ref_high, jc_close, ref_low, ref_high,
            )
            if self._allow_alert(pair, "FAKEOUT_HIGH"):
                events.append(AlertEvent(
                    pair=pair, alert_type="FAKEOUT_HIGH",
                    close_price=jc_close, swept_level=ref_high,
                    ref_high=ref_high, ref_low=ref_low,
                    jc_high=jc_high, jc_low=jc_low,
                    candle_time_ms=candle_time,
                ))

        # ── FAKEOUT_LOW ──────────────────────────────────────────────────────
        # Condition: ohlcv[-2].low < ohlcv[-3].low   (wick swept below)
        #        AND ohlcv[-3].low <= ohlcv[-2].close <= ohlcv[-3].high  (close inside)
        if jc_low < ref_low and ref_low <= jc_close <= ref_high:
            logger.info(
                "FAKEOUT_LOW %s | jc.low=%.4f < ref.low=%.4f | jc.close=%.4f ∈ [%.4f, %.4f]",
                pair, jc_low, ref_low, jc_close, ref_low, ref_high,
            )
            if self._allow_alert(pair, "FAKEOUT_LOW"):
                events.append(AlertEvent(
                    pair=pair, alert_type="FAKEOUT_LOW",
                    close_price=jc_close, swept_level=ref_low,
                    ref_high=ref_high, ref_low=ref_low,
                    jc_high=jc_high, jc_low=jc_low,
                    candle_time_ms=candle_time,
                ))

        return events

    # -------------------------------------------------------------------------

    def _allow_alert(self, pair: str, alert_type: str) -> bool:
        """Enforce per-pair-per-type cooldown to avoid duplicate alerts."""
        now = time.time()
        key = f"{pair}:{alert_type}"
        if now - self._last_alert_time.get(key, 0) >= self._cooldown:
            self._last_alert_time[key] = now
            return True
        return False
