"""
Candle Builder module for the Fyers Trading Bot.

Aggregates raw tick data into OHLCV candles of configurable timeframe.
Maintains a rolling history of the last 100 candles per symbol as a
pandas DataFrame. Thread-safe via threading.Lock.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from config import CANDLE_TIMEFRAME_MINUTES
from utils.time_utils import IST, get_ist_now, timestamp_to_ist

logger = logging.getLogger(__name__)

# Maximum number of historical candles to keep per symbol
MAX_HISTORY = 100


class CandleBuilder:
    """
    Aggregates ticks into OHLCV candles and maintains rolling history.

    On each tick, the current open candle is updated. When a new candle
    period starts, the previous candle is finalized and emitted via
    the on_candle_close callback.
    """

    def __init__(
        self,
        timeframe_minutes: int = CANDLE_TIMEFRAME_MINUTES,
        on_candle_close: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        """
        Initialize the CandleBuilder.

        Args:
            timeframe_minutes: Duration of each candle in minutes.
            on_candle_close: Callback function(symbol, candle_dict) called
                             when a candle is finalized.
        """
        self._timeframe = timeframe_minutes
        self._on_candle_close = on_candle_close
        self._lock = threading.Lock()

        # {symbol: current open candle dict}
        self._current_candles: dict[str, dict] = {}

        # {symbol: pd.DataFrame of completed candles}
        self._history: dict[str, pd.DataFrame] = {}

        logger.info(
            "CandleBuilder initialized — timeframe=%d min, max_history=%d",
            timeframe_minutes,
            MAX_HISTORY,
        )

    def _get_candle_start(self, dt: datetime) -> datetime:
        """
        Calculate the start time of the candle period that contains `dt`.

        Args:
            dt: IST-aware datetime.

        Returns:
            IST-aware datetime representing the candle period start.
        """
        # Floor to the nearest timeframe boundary
        minute = (dt.minute // self._timeframe) * self._timeframe
        return dt.replace(minute=minute, second=0, microsecond=0)

    def process_tick(
        self,
        symbol: str,
        price: float,
        volume: float,
        timestamp: float,
    ) -> None:
        """
        Process a single tick and update the current candle.

        If the tick falls into a new candle period, the previous candle is
        finalized and emitted via the on_candle_close callback.

        Args:
            symbol: Trading symbol (e.g. "NSE:NIFTY50-INDEX").
            price: Last traded price.
            volume: Tick volume.
            timestamp: Unix epoch timestamp of the tick.
        """
        tick_time = timestamp_to_ist(timestamp)
        candle_start = self._get_candle_start(tick_time)

        with self._lock:
            current = self._current_candles.get(symbol)

            if current is None:
                # First tick for this symbol — start a new candle
                self._current_candles[symbol] = self._new_candle(
                    symbol, price, volume, candle_start,
                )
                return

            if candle_start > current["timestamp"]:
                # New candle period — finalize the old candle
                completed_candle = self._finalize_candle(current)
                self._append_to_history(symbol, completed_candle)

                # Start a new candle
                self._current_candles[symbol] = self._new_candle(
                    symbol, price, volume, candle_start,
                )

                # Emit the completed candle
                logger.info(
                    "Candle close %s | O=%.2f H=%.2f L=%.2f C=%.2f | %s",
                    symbol,
                    completed_candle["open"],
                    completed_candle["high"],
                    completed_candle["low"],
                    completed_candle["close"],
                    completed_candle["timestamp"].strftime("%H:%M"),
                )
                if self._on_candle_close:
                    try:
                        self._on_candle_close(symbol, completed_candle)
                    except Exception as e:
                        logger.error(
                            "Error in on_candle_close callback for %s: %s",
                            symbol, e,
                        )
            else:
                # Same candle period — update OHLCV
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
                current["volume"] += volume
                current["tick_count"] += 1

    @staticmethod
    def _new_candle(
        symbol: str,
        price: float,
        volume: float,
        candle_start: datetime,
    ) -> dict:
        """Create a new candle dict from the first tick."""
        return {
            "symbol": symbol,
            "timestamp": candle_start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "tick_count": 1,
        }

    @staticmethod
    def _finalize_candle(candle: dict) -> dict:
        """Return a copy of the candle dict for emission (no mutation)."""
        return {
            "timestamp": candle["timestamp"],
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
        }

    def _append_to_history(self, symbol: str, candle: dict) -> None:
        """
        Append a completed candle to the rolling history DataFrame.

        Trims to MAX_HISTORY rows. Must be called within the lock.
        """
        new_row = pd.DataFrame([candle])

        if symbol not in self._history:
            self._history[symbol] = new_row
        else:
            self._history[symbol] = pd.concat(
                [self._history[symbol], new_row],
                ignore_index=True,
            )
            # Trim to max history
            if len(self._history[symbol]) > MAX_HISTORY:
                self._history[symbol] = (
                    self._history[symbol].iloc[-MAX_HISTORY:].reset_index(drop=True)
                )

    def get_history(self, symbol: str) -> pd.DataFrame:
        """
        Get the rolling candle history for a symbol.

        Args:
            symbol: Trading symbol.

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume.
            Returns an empty DataFrame if no history exists.
        """
        with self._lock:
            if symbol in self._history:
                return self._history[symbol].copy()
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )

    def get_current_candle(self, symbol: str) -> Optional[dict]:
        """
        Get the current (incomplete) candle for a symbol.

        Args:
            symbol: Trading symbol.

        Returns:
            Current candle dict or None if no candle is in progress.
        """
        with self._lock:
            candle = self._current_candles.get(symbol)
            return dict(candle) if candle else None

    def reset(self, symbol: Optional[str] = None) -> None:
        """
        Reset candle state for one or all symbols.

        Args:
            symbol: Specific symbol to reset, or None to reset all.
        """
        with self._lock:
            if symbol:
                self._current_candles.pop(symbol, None)
                self._history.pop(symbol, None)
                logger.info("CandleBuilder reset for %s", symbol)
            else:
                self._current_candles.clear()
                self._history.clear()
                logger.info("CandleBuilder reset for all symbols")
