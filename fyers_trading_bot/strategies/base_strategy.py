"""
Abstract base class for all trading strategies.

Every strategy must inherit from BaseStrategy and implement the `analyze`
method, which returns a signal dict on setup detection or None if no setup.
"""

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd


class BaseStrategy(ABC):
    """
    Abstract base class that all trading strategies must inherit from.

    Subclasses must implement:
        - name (property): Human-readable strategy name.
        - analyze(symbol, df): Returns a signal dict or None.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the human-readable name of the strategy."""
        ...

    @abstractmethod
    def analyze(self, symbol: str, df: pd.DataFrame) -> Optional[dict]:
        """
        Analyze candle history and detect trade setups.

        Args:
            symbol: Trading symbol (e.g. "NSE:NIFTY50-INDEX").
            df: DataFrame of OHLCV candles with columns:
                timestamp, open, high, low, close, volume.
                Sorted by timestamp ascending. Contains up to 100 candles.

        Returns:
            A signal dict on setup detection:
            {
                "symbol": str,
                "direction": "BUY" or "SELL",
                "entry_price": float,
                "sl": float,
                "target": float,
                "strategy": str,
                "reason": str,
                "confidence_score": float  (optional, 0.0–4.0)
            }
            Returns None if no setup is found.
        """
        ...
