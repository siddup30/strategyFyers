"""
Strategy Manager for the Fyers Trading Bot.

Loads and runs all active strategies. Routes candle close events to each
strategy, collects signals, and forwards valid signals to the risk manager
and order execution pipeline.
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from config import ACTIVE_STRATEGIES, SIGNAL_COOLDOWN_MINUTES
from strategies.base_strategy import BaseStrategy
from strategies.range_breakout import RangeBreakoutStrategy
from strategies.smc_strategy import SMCStrategy

logger = logging.getLogger(__name__)

# ─── Strategy Registry ───────────────────────────────────────────────────────────
_STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "range_breakout": RangeBreakoutStrategy,
    "smc": SMCStrategy,
}


class StrategyManager:
    """
    Manages all active trading strategies.

    Loads strategies based on config.ACTIVE_STRATEGIES, routes candle close
    events to each strategy, and collects signals for further processing.
    """

    def __init__(
        self,
        on_signal: Optional[callable] = None,
    ) -> None:
        """
        Initialize the StrategyManager.

        Args:
            on_signal: Callback function(signal_dict) called when a strategy
                       generates a valid signal.
        """
        self._strategies: list[BaseStrategy] = []
        self._on_signal = on_signal
        self._last_signal_time: dict[str, datetime] = {}  # symbol → last signal time
        self._load_strategies()

    def _load_strategies(self) -> None:
        """Load and instantiate all active strategies from config."""
        for strategy_name in ACTIVE_STRATEGIES:
            strategy_class = _STRATEGY_REGISTRY.get(strategy_name)
            if strategy_class is None:
                logger.warning(
                    "Unknown strategy '%s' in ACTIVE_STRATEGIES — skipping.",
                    strategy_name,
                )
                continue

            strategy = strategy_class()
            self._strategies.append(strategy)
            logger.info("Strategy loaded: %s", strategy.name)

        logger.info(
            "StrategyManager initialized with %d active strategies: %s",
            len(self._strategies),
            [s.name for s in self._strategies],
        )

    def on_candle_close(self, symbol: str, candle: dict) -> None:
        """
        Handle a completed candle event.

        Routes the candle to all active strategies and collects any signals.

        Args:
            symbol: Trading symbol.
            candle: Finalized OHLCV candle dict.
        """
        logger.debug(
            "Candle close for %s: O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            symbol,
            candle.get("open", 0),
            candle.get("high", 0),
            candle.get("low", 0),
            candle.get("close", 0),
            candle.get("volume", 0),
        )

    def evaluate(self, symbol: str, df: pd.DataFrame) -> list[dict]:
        """
        Run all strategies against the latest candle history.

        Called after a candle close event. Each strategy's analyze() method
        is invoked with the full candle history DataFrame.

        Args:
            symbol: Trading symbol.
            df: OHLCV DataFrame with full candle history for the symbol.

        Returns:
            List of signal dicts from all strategies that fired.
        """
        signals: list[dict] = []

        for strategy in self._strategies:
            try:
                signal = strategy.analyze(symbol, df)
                if signal is not None:
                    # ── Signal cooldown: skip if within SIGNAL_COOLDOWN_MINUTES ──
                    now = df.iloc[-1]["timestamp"] if "timestamp" in df.columns else None
                    last = self._last_signal_time.get(symbol)
                    if now and last:
                        elapsed = (now - last).total_seconds() / 60
                        if elapsed < SIGNAL_COOLDOWN_MINUTES:
                            logger.info(
                                "Signal cooldown active for %s — %.1f min since last signal (need %d min)",
                                symbol, elapsed, SIGNAL_COOLDOWN_MINUTES,
                            )
                            continue

                    logger.info(
                        "Signal from %s: %s %s @ %.2f",
                        strategy.name,
                        signal["direction"],
                        signal["symbol"],
                        signal["entry_price"],
                    )
                    signals.append(signal)
                    if now:
                        self._last_signal_time[symbol] = now

                    # Forward to callback
                    if self._on_signal:
                        self._on_signal(signal)

            except Exception as e:
                logger.error(
                    "Error running strategy '%s' for %s: %s",
                    strategy.name,
                    symbol,
                    e,
                    exc_info=True,
                )

        return signals

    @property
    def strategy_names(self) -> list[str]:
        """Return names of all loaded strategies."""
        return [s.name for s in self._strategies]

    @staticmethod
    def register_strategy(name: str, strategy_class: type[BaseStrategy]) -> None:
        """
        Register a new strategy class in the global registry.

        Args:
            name: Unique strategy name (used in config.ACTIVE_STRATEGIES).
            strategy_class: Class that inherits from BaseStrategy.
        """
        _STRATEGY_REGISTRY[name] = strategy_class
        logger.info("Strategy '%s' registered.", name)
