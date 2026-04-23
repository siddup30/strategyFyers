"""
Risk Management module for the Fyers Trading Bot.

Enforces position sizing, daily loss caps, trade count limits, and
reward-risk ratio filters. All signals must pass validation before
orders are placed.
"""

import logging
import math
import threading
from typing import Optional

from config import (
    CAPITAL,
    ENTRY_CUTOFF,
    LOT_SIZES,
    MARKET_CLOSE,
    MARKET_OPEN,
    MAX_DAILY_LOSS_INR,
    MAX_POSITIONS_PER_SYMBOL,
    MAX_TRADES_PER_DAY,
    MIN_REWARD_RISK_RATIO,
    OPTION_SL_PCT,
    RISK_PER_TRADE_PCT,
)
from utils.time_utils import get_ist_now, get_today_date_str, is_market_open

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Centralized risk management for all trade signals.

    Tracks daily PnL and trade count, validates signals against risk rules,
    and calculates position sizes. Thread-safe via threading.Lock.
    """

    def __init__(self) -> None:
        """Initialize the RiskManager with zeroed daily counters."""
        self._lock = threading.Lock()
        self._daily_loss: float = 0.0
        self._trade_count: int = 0
        self._last_reset_date: str = get_today_date_str()

        logger.info(
            "RiskManager initialized — capital=₹%.0f, risk_per_trade=%.1f%%, "
            "max_daily_loss=₹%.0f, max_trades=%d, min_rr=%.1f",
            CAPITAL,
            RISK_PER_TRADE_PCT,
            MAX_DAILY_LOSS_INR,
            MAX_TRADES_PER_DAY,
            MIN_REWARD_RISK_RATIO,
        )

    def _check_reset(self) -> None:
        """Reset daily counters if the date has changed. Must hold lock."""
        today = get_today_date_str()
        if today != self._last_reset_date:
            logger.info(
                "New trading day detected (%s → %s). Resetting daily counters.",
                self._last_reset_date,
                today,
            )
            self._daily_loss = 0.0
            self._trade_count = 0
            self._last_reset_date = today

    def validate_signal(
        self,
        signal: dict,
        open_positions: Optional[list] = None,
    ) -> tuple[bool, str]:
        """
        Validate a trading signal against all risk rules.

        Checks:
        1. Entry cutoff time (no new entries after ENTRY_CUTOFF)
        2. Max positions per symbol
        3. Reward-risk ratio >= MIN_REWARD_RISK_RATIO
        4. Daily loss < MAX_DAILY_LOSS_INR
        5. Trade count < MAX_TRADES_PER_DAY
        6. Market is open

        Args:
            signal: Signal dict with entry_price, sl, target, symbol.
            open_positions: List of currently open position dicts (for stacking check).

        Returns:
            Tuple of (is_valid: bool, reason: str).
        """
        with self._lock:
            self._check_reset()

            now = get_ist_now()

            # ─── Check entry cutoff ───────────────────────────────────────
            cutoff_h, cutoff_m = map(int, ENTRY_CUTOFF.split(":"))
            if now.hour > cutoff_h or (now.hour == cutoff_h and now.minute >= cutoff_m):
                return False, f"Entry cutoff reached ({ENTRY_CUTOFF} IST)"

            # ─── Check per-symbol position limit ─────────────────────────
            if open_positions is not None:
                symbol = signal.get("symbol", "")
                symbol_count = sum(
                    1 for p in open_positions
                    if p.get("index_symbol") == symbol or p.get("symbol") == symbol
                )
                if symbol_count >= MAX_POSITIONS_PER_SYMBOL:
                    return False, (
                        f"Max positions ({MAX_POSITIONS_PER_SYMBOL}) reached for {symbol}"
                    )

            entry = signal["entry_price"]
            sl = signal["sl"]
            target = signal["target"]

            # ─── Check RR ratio ───────────────────────────────────────────
            risk = abs(entry - sl)
            reward = abs(target - entry)

            if risk <= 0:
                return False, "Risk is zero or negative (entry == sl)"

            rr_ratio = reward / risk
            if rr_ratio < MIN_REWARD_RISK_RATIO:
                return False, (
                    f"RR ratio {rr_ratio:.2f} below minimum {MIN_REWARD_RISK_RATIO:.1f}"
                )

            # ─── Check daily loss cap ─────────────────────────────────────
            if abs(self._daily_loss) >= MAX_DAILY_LOSS_INR:
                return False, (
                    f"Daily loss cap reached: ₹{abs(self._daily_loss):,.0f} "
                    f">= ₹{MAX_DAILY_LOSS_INR:,.0f}"
                )

            # ─── Check trade count ────────────────────────────────────────
            if self._trade_count >= MAX_TRADES_PER_DAY:
                return False, (
                    f"Max trades per day reached: {self._trade_count} "
                    f">= {MAX_TRADES_PER_DAY}"
                )

            # ─── Check market hours ──────────────────────────────────────
            if not is_market_open(MARKET_OPEN, MARKET_CLOSE):
                return False, "Market is closed"

            return True, "ok"

    def calculate_lot_size(
        self,
        symbol: str,
        entry_premium: float,
    ) -> int:
        """
        Calculate position size in lots based on option premium risk.

        Formula:
            risk_per_trade = CAPITAL × (RISK_PER_TRADE_PCT / 100)
            premium_at_risk = entry_premium × (OPTION_SL_PCT / 100)
            lots = floor(risk_per_trade / (premium_at_risk × lot_size_unit))

        Args:
            symbol: Index symbol (must be in LOT_SIZES).
            entry_premium: Option entry premium.

        Returns:
            Number of lots (minimum 1).
        """
        risk_per_trade = CAPITAL * (RISK_PER_TRADE_PCT / 100.0)
        premium_at_risk = entry_premium * (OPTION_SL_PCT / 100.0)

        if premium_at_risk <= 0:
            logger.warning("Premium at risk is zero for %s. Returning 1 lot.", symbol)
            return 1

        lot_size_unit = LOT_SIZES.get(symbol, 65)
        risk_per_lot = premium_at_risk * lot_size_unit
        lots = math.floor(risk_per_trade / risk_per_lot)

        result = max(1, lots)

        logger.info(
            "Position sizing for %s: premium=%.2f, sl_pct=%.1f%%, premium_risk=%.2f, "
            "risk_per_trade=₹%.0f, lot_unit=%d, lots=%d",
            symbol, entry_premium, OPTION_SL_PCT, premium_at_risk,
            risk_per_trade, lot_size_unit, result,
        )
        return result

    def record_trade_result(self, pnl: float) -> None:
        """
        Record the PnL of a completed trade and update daily counters.

        Args:
            pnl: Profit (positive) or loss (negative) in INR.
        """
        with self._lock:
            self._check_reset()
            self._daily_loss += pnl  # pnl is negative for losses
            self._trade_count += 1

            logger.info(
                "Trade result recorded: PnL=₹%.2f | Daily PnL=₹%.2f | "
                "Trades today=%d/%d",
                pnl,
                self._daily_loss,
                self._trade_count,
                MAX_TRADES_PER_DAY,
            )

    def is_trading_allowed(self) -> bool:
        """
        Check if trading is currently allowed.

        Returns:
            True if market is open, daily loss cap not hit, and trade
            count not exceeded.
        """
        with self._lock:
            self._check_reset()

            if not is_market_open(MARKET_OPEN, MARKET_CLOSE):
                return False

            if abs(self._daily_loss) >= MAX_DAILY_LOSS_INR:
                return False

            if self._trade_count >= MAX_TRADES_PER_DAY:
                return False

            return True

    @property
    def daily_pnl(self) -> float:
        """Return the current daily PnL in INR."""
        with self._lock:
            return self._daily_loss

    @property
    def trade_count_today(self) -> int:
        """Return the number of trades taken today."""
        with self._lock:
            return self._trade_count

    def get_status(self) -> dict:
        """
        Get current risk manager status.

        Returns:
            Dict with daily_pnl, trade_count, max_trades, daily_loss_cap,
            and trading_allowed.
        """
        with self._lock:
            self._check_reset()
            return {
                "daily_pnl": round(self._daily_loss, 2),
                "trade_count": self._trade_count,
                "max_trades": MAX_TRADES_PER_DAY,
                "daily_loss_cap": MAX_DAILY_LOSS_INR,
                "trading_allowed": self.is_trading_allowed(),
            }
