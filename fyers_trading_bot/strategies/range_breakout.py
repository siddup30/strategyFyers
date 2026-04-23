"""
Opening Range Breakout (ORB) strategy for the Fyers Trading Bot.

Detects breakouts from the first 30 minutes of trading (9:15–9:45 IST).
Includes Previous Day High/Low (PDH/PDL) confluence for higher confidence.

Rules:
- Opening range = high/low of the first 6 candles (5-min) after 9:15 AM.
- After 9:45 AM, signal on candle close breaking above/below the range.
- Volume must exceed the 20-period average volume.
- One signal per symbol per day; no signals after 2:00 PM IST.
- SL at opposite side of the range; target = entry ± 1.5× range size.
"""

import logging
from datetime import time
from typing import Optional

import pandas as pd

from strategies.base_strategy import BaseStrategy
from utils.time_utils import get_ist_now, IST

logger = logging.getLogger(__name__)

# ─── Strategy Constants ──────────────────────────────────────────────────────────
ORB_CANDLES = 6          # first 6 candles of 5-min data = 30 minutes
ORB_START = time(9, 15)  # market opens
ORB_END = time(9, 45)    # opening range formation complete
SIGNAL_CUTOFF = time(14, 0)  # no signals after 2 PM
VOLUME_LOOKBACK = 20     # candles for average volume
TARGET_MULTIPLIER = 1.5  # target = entry ± range_size × multiplier


class RangeBreakoutStrategy(BaseStrategy):
    """
    Opening Range Breakout (ORB) strategy with PDH/PDL confluence.

    Tracks the opening range per symbol per day and generates a single
    breakout signal when price closes beyond the range with volume
    confirmation.
    """

    def __init__(self) -> None:
        """Initialize the ORB strategy."""
        self._orb_ranges: dict[str, dict] = {}  # {symbol: {high, low, date}}
        self._fired_today: dict[str, str] = {}  # {symbol: date_str}
        self._pdh_pdl: dict[str, dict] = {}     # {symbol: {pdh, pdl}}

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "range_breakout"

    def analyze(self, symbol: str, df: pd.DataFrame) -> Optional[dict]:
        """
        Analyze candle data for ORB setups.

        Args:
            symbol: Trading symbol.
            df: OHLCV DataFrame with at least 6 candles.

        Returns:
            Signal dict or None.
        """
        if df.empty or len(df) < ORB_CANDLES:
            return None

        now = get_ist_now()
        today_str = now.strftime("%Y-%m-%d")
        current_time = now.time()

        # ─── Reset fired flag at new day ──────────────────────────────────
        if self._fired_today.get(symbol) != today_str:
            if now.time() >= ORB_START:
                self._fired_today.pop(symbol, None)
                self._orb_ranges.pop(symbol, None)

        # ─── Already fired today — skip ──────────────────────────────────
        if self._fired_today.get(symbol) == today_str:
            return None

        # ─── No signals after cutoff ─────────────────────────────────────
        if current_time >= SIGNAL_CUTOFF:
            return None

        # ─── Build or retrieve the opening range ─────────────────────────
        orb = self._get_or_build_orb(symbol, df, today_str)
        if orb is None:
            return None

        # ─── Don't signal during the opening range formation ─────────────
        if current_time < ORB_END:
            return None

        # ─── Get PDH / PDL for confluence ────────────────────────────────
        pdh_pdl = self._get_pdh_pdl(symbol, df)

        # ─── Check the latest candle for breakout ────────────────────────
        latest = df.iloc[-1]
        close = latest["close"]
        volume = latest["volume"]

        # Volume confirmation: current volume > 20-period average
        avg_volume = df["volume"].tail(VOLUME_LOOKBACK).mean()
        volume_confirmed = volume > avg_volume if avg_volume > 0 else True

        if not volume_confirmed:
            return None

        orb_high = orb["high"]
        orb_low = orb["low"]
        range_size = orb_high - orb_low

        if range_size <= 0:
            return None

        signal = None

        # ─── Bullish breakout ─────────────────────────────────────────────
        if close > orb_high:
            entry = close
            sl = orb_low
            target = entry + (range_size * TARGET_MULTIPLIER)
            confidence = self._calc_confidence(
                "BUY", close, pdh_pdl, orb_high, volume, avg_volume,
            )

            signal = {
                "symbol": symbol,
                "direction": "BUY",
                "entry_price": round(entry, 2),
                "sl": round(sl, 2),
                "target": round(target, 2),
                "strategy": self.name,
                "reason": self._build_reason("BUY", orb_high, orb_low, pdh_pdl),
                "confidence_score": confidence,
            }

        # ─── Bearish breakout ─────────────────────────────────────────────
        elif close < orb_low:
            entry = close
            sl = orb_high
            target = entry - (range_size * TARGET_MULTIPLIER)
            confidence = self._calc_confidence(
                "SELL", close, pdh_pdl, orb_low, volume, avg_volume,
            )

            signal = {
                "symbol": symbol,
                "direction": "SELL",
                "entry_price": round(entry, 2),
                "sl": round(sl, 2),
                "target": round(target, 2),
                "strategy": self.name,
                "reason": self._build_reason("SELL", orb_high, orb_low, pdh_pdl),
                "confidence_score": confidence,
            }

        if signal:
            self._fired_today[symbol] = today_str
            logger.info(
                "ORB signal: %s %s @ %.2f (SL: %.2f, T: %.2f, Conf: %.1f)",
                signal["direction"], symbol, signal["entry_price"],
                signal["sl"], signal["target"], signal["confidence_score"],
            )

        return signal

    def _get_or_build_orb(
        self,
        symbol: str,
        df: pd.DataFrame,
        today_str: str,
    ) -> Optional[dict]:
        """
        Build the opening range from the first ORB_CANDLES candles of today.

        Returns:
            Dict with 'high' and 'low' keys, or None if not enough data.
        """
        # Check if already computed for today
        existing = self._orb_ranges.get(symbol)
        if existing and existing.get("date") == today_str:
            return existing

        # Filter candles for today's session
        today_candles = self._get_today_candles(df, today_str)
        if len(today_candles) < ORB_CANDLES:
            return None

        # First 6 candles form the opening range
        orb_candles = today_candles.head(ORB_CANDLES)
        orb = {
            "high": float(orb_candles["high"].max()),
            "low": float(orb_candles["low"].min()),
            "date": today_str,
        }
        self._orb_ranges[symbol] = orb
        logger.info(
            "ORB range for %s: High=%.2f, Low=%.2f (range=%.2f)",
            symbol, orb["high"], orb["low"], orb["high"] - orb["low"],
        )
        return orb

    def _get_pdh_pdl(self, symbol: str, df: pd.DataFrame) -> dict:
        """
        Get Previous Day High and Previous Day Low from candle history.

        Returns:
            Dict with 'pdh' and 'pdl' keys. Values are 0.0 if not available.
        """
        if symbol in self._pdh_pdl:
            return self._pdh_pdl[symbol]

        today_str = get_ist_now().strftime("%Y-%m-%d")

        # Get candles that are NOT from today
        if "timestamp" in df.columns and not df.empty:
            prev_candles = df[
                df["timestamp"].apply(
                    lambda t: t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
                ) != today_str
            ]
            if not prev_candles.empty:
                pdh_pdl = {
                    "pdh": float(prev_candles["high"].max()),
                    "pdl": float(prev_candles["low"].min()),
                }
                self._pdh_pdl[symbol] = pdh_pdl
                logger.info("PDH/PDL for %s: PDH=%.2f, PDL=%.2f", symbol, pdh_pdl["pdh"], pdh_pdl["pdl"])
                return pdh_pdl

        return {"pdh": 0.0, "pdl": 0.0}

    @staticmethod
    def _get_today_candles(df: pd.DataFrame, today_str: str) -> pd.DataFrame:
        """Filter DataFrame for today's candles only."""
        if "timestamp" in df.columns and not df.empty:
            mask = df["timestamp"].apply(
                lambda t: t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
            ) == today_str
            return df[mask].reset_index(drop=True)
        return pd.DataFrame()

    @staticmethod
    def _calc_confidence(
        direction: str,
        close: float,
        pdh_pdl: dict,
        breakout_level: float,
        volume: float,
        avg_volume: float,
    ) -> float:
        """
        Calculate confidence score (0–4) based on confluences.

        +1 for ORB breakout (always present if we reach here)
        +1 for volume spike (> 1.5× average)
        +1 for PDH confluence (BUY above PDH or SELL below PDL)
        +1 for strong close (>= 0.5% beyond breakout level)
        """
        score = 1.0  # ORB breakout is always present

        # Volume spike
        if avg_volume > 0 and volume > (avg_volume * 1.5):
            score += 1.0

        # PDH/PDL confluence
        pdh = pdh_pdl.get("pdh", 0.0)
        pdl = pdh_pdl.get("pdl", 0.0)
        if direction == "BUY" and pdh > 0 and close > pdh:
            score += 1.0
        elif direction == "SELL" and pdl > 0 and close < pdl:
            score += 1.0

        # Strong close beyond breakout level
        if breakout_level > 0:
            move_pct = abs(close - breakout_level) / breakout_level * 100
            if move_pct >= 0.5:
                score += 1.0

        return score

    @staticmethod
    def _build_reason(
        direction: str,
        orb_high: float,
        orb_low: float,
        pdh_pdl: dict,
    ) -> str:
        """Build a human-readable reason string for the signal."""
        parts = []
        if direction == "BUY":
            parts.append(f"Close broke above ORB high {orb_high:.2f}")
            if pdh_pdl.get("pdh", 0) > 0:
                parts.append(f"PDH: {pdh_pdl['pdh']:.2f}")
        else:
            parts.append(f"Close broke below ORB low {orb_low:.2f}")
            if pdh_pdl.get("pdl", 0) > 0:
                parts.append(f"PDL: {pdh_pdl['pdl']:.2f}")
        parts.append("Volume confirmed")
        return " | ".join(parts)
