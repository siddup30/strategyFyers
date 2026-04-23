"""
Smart Money Concepts (SMC) strategy for the Fyers Trading Bot.

Detects institutional order flow patterns:
1. Market Structure — Break of Structure (BOS) & Change of Character (CHoCH)
2. Order Blocks — Last opposing candle before a BOS
3. Fair Value Gaps (FVG) — 3-candle imbalance zones
4. Confluence scoring — signals only when score >= 3

Entry: price retraces into a valid OB/FVG zone with matching bias.
"""

import logging
from typing import Optional

import pandas as pd

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# ─── Strategy Constants ──────────────────────────────────────────────────────────
SWING_LOOKBACK = 3     # candles to look back/forward for swing point detection
FVG_LOOKBACK = 20      # max candles to search for unfilled FVGs
SL_BUFFER = 5.0        # points buffer for stop loss beyond OB boundary
MIN_CONFLUENCE = 3     # minimum confluence score to generate a signal


class SMCStrategy(BaseStrategy):
    """
    Smart Money Concepts (SMC) strategy.

    Identifies institutional order flow through market structure analysis,
    order block detection, and fair value gap identification. Generates
    signals only when confluence score meets the minimum threshold.
    """

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "smc"

    def analyze(self, symbol: str, df: pd.DataFrame) -> Optional[dict]:
        """
        Analyze candle data for SMC trade setups.

        Args:
            symbol: Trading symbol.
            df: OHLCV DataFrame with sufficient history.

        Returns:
            Signal dict or None.
        """
        if df.empty or len(df) < SWING_LOOKBACK * 2 + 5:
            return None

        try:
            return self._generate_signal(symbol, df)
        except Exception as e:
            logger.error("SMC analysis error for %s: %s", symbol, e)
            return None

    def _generate_signal(self, symbol: str, df: pd.DataFrame) -> Optional[dict]:
        """
        Core signal generation logic combining all SMC components.

        Args:
            symbol: Trading symbol.
            df: OHLCV DataFrame.

        Returns:
            Signal dict or None.
        """
        # Step 1: Detect market structure bias
        bias, swing_highs, swing_lows = detect_market_structure(df)
        if bias == "neutral":
            return None

        # Step 2: Detect order blocks
        ob = detect_order_blocks(df, bias)

        # Step 3: Detect fair value gaps
        fvgs = detect_fvg(df)

        # Step 4: Check if current price is retracing into OB or FVG
        latest = df.iloc[-1]
        close = latest["close"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(20).mean()

        signal = None

        if bias == "bullish":
            signal = self._check_bullish_entry(
                symbol, df, close, volume, avg_volume,
                ob, fvgs, bias, swing_highs, swing_lows,
            )
        elif bias == "bearish":
            signal = self._check_bearish_entry(
                symbol, df, close, volume, avg_volume,
                ob, fvgs, bias, swing_highs, swing_lows,
            )

        return signal

    def _check_bullish_entry(
        self,
        symbol: str,
        df: pd.DataFrame,
        close: float,
        volume: float,
        avg_volume: float,
        ob: Optional[dict],
        fvgs: list[dict],
        bias: str,
        swing_highs: list[float],
        swing_lows: list[float],
    ) -> Optional[dict]:
        """Check for bullish entry conditions."""
        confluence = 0
        reasons = []

        # Check if price is in a bullish OB zone
        in_ob = False
        if ob and ob["type"] == "bullish":
            if ob["bottom"] <= close <= ob["top"]:
                confluence += 1
                in_ob = True
                reasons.append(f"Price in bullish OB [{ob['bottom']:.2f}–{ob['top']:.2f}]")

        # Check if price is in a bullish FVG
        in_fvg = False
        matching_fvg = None
        for fvg in fvgs:
            if fvg["type"] == "bullish" and fvg["bottom"] <= close <= fvg["top"]:
                confluence += 1
                in_fvg = True
                matching_fvg = fvg
                reasons.append(f"Price in bullish FVG [{fvg['bottom']:.2f}–{fvg['top']:.2f}]")
                break

        # Bias confirmation
        if bias == "bullish":
            confluence += 1
            reasons.append("Bullish market structure (BOS)")

        # Volume spike on entry candle
        if avg_volume > 0 and volume > avg_volume * 1.2:
            confluence += 1
            reasons.append("Volume spike on entry candle")

        # Need at least one zone (OB or FVG) and minimum confluence
        if not (in_ob or in_fvg) or confluence < MIN_CONFLUENCE:
            return None

        # Calculate SL and target
        if ob and ob["type"] == "bullish":
            sl = ob["bottom"] - SL_BUFFER
        elif matching_fvg:
            sl = matching_fvg["bottom"] - SL_BUFFER
        else:
            return None

        # Target: next swing high
        target = self._find_next_swing_target(swing_highs, close, "BUY")
        if target is None:
            # Fallback: 2× risk
            risk = abs(close - sl)
            target = close + (risk * 2)

        # Verify RR >= 2:1
        risk = abs(close - sl)
        reward = abs(target - close)
        if risk <= 0 or reward / risk < 2.0:
            return None

        signal = {
            "symbol": symbol,
            "direction": "BUY",
            "entry_price": round(close, 2),
            "sl": round(sl, 2),
            "target": round(target, 2),
            "strategy": self.name,
            "reason": " | ".join(reasons),
            "confidence_score": float(confluence),
        }

        logger.info(
            "SMC BUY signal: %s @ %.2f (SL: %.2f, T: %.2f, Conf: %d)",
            symbol, close, sl, target, confluence,
        )
        return signal

    def _check_bearish_entry(
        self,
        symbol: str,
        df: pd.DataFrame,
        close: float,
        volume: float,
        avg_volume: float,
        ob: Optional[dict],
        fvgs: list[dict],
        bias: str,
        swing_highs: list[float],
        swing_lows: list[float],
    ) -> Optional[dict]:
        """Check for bearish entry conditions."""
        confluence = 0
        reasons = []

        # Check if price is in a bearish OB zone
        in_ob = False
        if ob and ob["type"] == "bearish":
            if ob["bottom"] <= close <= ob["top"]:
                confluence += 1
                in_ob = True
                reasons.append(f"Price in bearish OB [{ob['bottom']:.2f}–{ob['top']:.2f}]")

        # Check if price is in a bearish FVG
        in_fvg = False
        matching_fvg = None
        for fvg in fvgs:
            if fvg["type"] == "bearish" and fvg["bottom"] <= close <= fvg["top"]:
                confluence += 1
                in_fvg = True
                matching_fvg = fvg
                reasons.append(f"Price in bearish FVG [{fvg['bottom']:.2f}–{fvg['top']:.2f}]")
                break

        # Bias confirmation
        if bias == "bearish":
            confluence += 1
            reasons.append("Bearish market structure (BOS)")

        # Volume spike on entry candle
        if avg_volume > 0 and volume > avg_volume * 1.2:
            confluence += 1
            reasons.append("Volume spike on entry candle")

        # Need at least one zone (OB or FVG) and minimum confluence
        if not (in_ob or in_fvg) or confluence < MIN_CONFLUENCE:
            return None

        # Calculate SL and target
        if ob and ob["type"] == "bearish":
            sl = ob["top"] + SL_BUFFER
        elif matching_fvg:
            sl = matching_fvg["top"] + SL_BUFFER
        else:
            return None

        # Target: next swing low
        target = self._find_next_swing_target(swing_lows, close, "SELL")
        if target is None:
            risk = abs(sl - close)
            target = close - (risk * 2)

        # Verify RR >= 2:1
        risk = abs(sl - close)
        reward = abs(close - target)
        if risk <= 0 or reward / risk < 2.0:
            return None

        signal = {
            "symbol": symbol,
            "direction": "SELL",
            "entry_price": round(close, 2),
            "sl": round(sl, 2),
            "target": round(target, 2),
            "strategy": self.name,
            "reason": " | ".join(reasons),
            "confidence_score": float(confluence),
        }

        logger.info(
            "SMC SELL signal: %s @ %.2f (SL: %.2f, T: %.2f, Conf: %d)",
            symbol, close, sl, target, confluence,
        )
        return signal

    @staticmethod
    def _find_next_swing_target(
        swings: list[float],
        current_price: float,
        direction: str,
    ) -> Optional[float]:
        """
        Find the next swing point as a target.

        For BUY: next swing high above current price.
        For SELL: next swing low below current price.
        """
        if direction == "BUY":
            targets = [s for s in swings if s > current_price]
            return min(targets) if targets else None
        else:
            targets = [s for s in swings if s < current_price]
            return max(targets) if targets else None


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level detector functions (used by SMCStrategy and potentially others)
# ═══════════════════════════════════════════════════════════════════════════════


def detect_market_structure(
    df: pd.DataFrame,
) -> tuple[str, list[float], list[float]]:
    """
    Identify swing highs/lows and detect Break of Structure (BOS).

    A swing high is a candle whose high is higher than the SWING_LOOKBACK
    candles on either side. A swing low is the opposite.

    BOS: price closes beyond the last swing high (bullish) or swing low (bearish).
    CHoCH: after a BOS, price reverses and breaks structure in the opposite direction.

    Args:
        df: OHLCV DataFrame.

    Returns:
        Tuple of (bias, swing_highs, swing_lows) where:
        - bias: "bullish", "bearish", or "neutral"
        - swing_highs: list of swing high prices
        - swing_lows: list of swing low prices
    """
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    swing_high_indices: list[int] = []
    swing_low_indices: list[int] = []

    n = len(df)

    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        # Swing high: higher than surrounding candles
        if all(highs[i] > highs[i - j] for j in range(1, SWING_LOOKBACK + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, SWING_LOOKBACK + 1)):
            swing_highs.append(float(highs[i]))
            swing_high_indices.append(i)

        # Swing low: lower than surrounding candles
        if all(lows[i] < lows[i - j] for j in range(1, SWING_LOOKBACK + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, SWING_LOOKBACK + 1)):
            swing_lows.append(float(lows[i]))
            swing_low_indices.append(i)

    if not swing_highs or not swing_lows:
        return "neutral", swing_highs, swing_lows

    last_close = float(closes[-1])
    last_swing_high = swing_highs[-1]
    last_swing_low = swing_lows[-1]

    # Detect BOS
    bullish_bos = last_close > last_swing_high
    bearish_bos = last_close < last_swing_low

    # Determine bias
    if bullish_bos and not bearish_bos:
        bias = "bullish"
    elif bearish_bos and not bullish_bos:
        bias = "bearish"
    else:
        # Check for CHoCH: look at the last two BOS directions
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            # If the latest swing high is higher than the previous → bullish trend
            if swing_highs[-1] > swing_highs[-2]:
                bias = "bullish"
            elif swing_lows[-1] < swing_lows[-2]:
                bias = "bearish"
            else:
                bias = "neutral"
        else:
            bias = "neutral"

    return bias, swing_highs, swing_lows


def detect_order_blocks(
    df: pd.DataFrame,
    bias: str,
) -> Optional[dict]:
    """
    Detect the most recent valid order block.

    Bullish OB: last bearish (red) candle before a bullish move.
    Bearish OB: last bullish (green) candle before a bearish move.

    Args:
        df: OHLCV DataFrame.
        bias: Current market bias ("bullish" or "bearish").

    Returns:
        Dict with keys: top, bottom, type, index — or None.
    """
    n = len(df)
    if n < 3:
        return None

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    # Search backwards for the most recent OB
    for i in range(n - 2, 0, -1):
        candle_is_bearish = closes[i] < opens[i]
        candle_is_bullish = closes[i] > opens[i]

        if bias == "bullish" and candle_is_bearish:
            # Check if the next candle(s) show a bullish move
            if i + 1 < n and closes[i + 1] > highs[i]:
                return {
                    "top": float(highs[i]),
                    "bottom": float(lows[i]),
                    "type": "bullish",
                    "index": i,
                }

        elif bias == "bearish" and candle_is_bullish:
            # Check if the next candle(s) show a bearish move
            if i + 1 < n and closes[i + 1] < lows[i]:
                return {
                    "top": float(highs[i]),
                    "bottom": float(lows[i]),
                    "type": "bearish",
                    "index": i,
                }

    return None


def detect_fvg(df: pd.DataFrame) -> list[dict]:
    """
    Detect Fair Value Gaps (FVGs) within the last FVG_LOOKBACK candles.

    Bullish FVG: candle[i-2].high < candle[i].low (gap up imbalance)
    Bearish FVG: candle[i-2].low > candle[i].high (gap down imbalance)

    Only returns unfilled FVGs (current price hasn't fully retraced through them).

    Args:
        df: OHLCV DataFrame.

    Returns:
        List of FVG dicts with keys: top, bottom, type, index.
    """
    n = len(df)
    start = max(2, n - FVG_LOOKBACK)
    fvgs: list[dict] = []

    highs = df["high"].values
    lows = df["low"].values
    last_close = float(df["close"].iloc[-1])

    for i in range(start, n):
        # Bullish FVG: gap between candle[i-2].high and candle[i].low
        if highs[i - 2] < lows[i]:
            fvg_bottom = float(highs[i - 2])
            fvg_top = float(lows[i])

            # Check if FVG is still unfilled (price hasn't dropped through it)
            if last_close >= fvg_bottom:
                fvgs.append({
                    "top": fvg_top,
                    "bottom": fvg_bottom,
                    "type": "bullish",
                    "index": i,
                })

        # Bearish FVG: gap between candle[i].high and candle[i-2].low
        if lows[i - 2] > highs[i]:
            fvg_top = float(lows[i - 2])
            fvg_bottom = float(highs[i])

            # Check if FVG is still unfilled (price hasn't rallied through it)
            if last_close <= fvg_top:
                fvgs.append({
                    "top": fvg_top,
                    "bottom": fvg_bottom,
                    "type": "bearish",
                    "index": i,
                })

    return fvgs
