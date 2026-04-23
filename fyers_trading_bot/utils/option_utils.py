"""
Option utilities for the Fyers Trading Bot.

Handles ATM strike calculation and Fyers option symbol construction.
Supports both NIFTY and BANKNIFTY weekly/monthly option chains.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from utils.time_utils import IST, get_ist_now

logger = logging.getLogger(__name__)

# ─── Strike Rounding Rules ──────────────────────────────────────────────────────
STRIKE_ROUNDING: dict[str, int] = {
    "NSE:NIFTY50-INDEX": 50,       # Nifty strikes at 50-point intervals
    "NSE:NIFTYBANK-INDEX": 100,    # BankNifty strikes at 100-point intervals
}

# ─── Underlying to Symbol Prefix Mapping ─────────────────────────────────────────
OPTION_PREFIX: dict[str, str] = {
    "NSE:NIFTY50-INDEX": "NSE:NIFTY",
    "NSE:NIFTYBANK-INDEX": "NSE:BANKNIFTY",
}

# ─── Option Lot Sizes ────────────────────────────────────────────────────────────
OPTION_LOT_SIZES: dict[str, int] = {
    "NSE:NIFTY50-INDEX": 65,
    "NSE:NIFTYBANK-INDEX": 30,
}

# ─── Month Code Mapping ─────────────────────────────────────────────────────────
MONTH_CODES: dict[int, str] = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def get_atm_strike(index_price: float, symbol: str) -> int:
    """
    Calculate the At-The-Money (ATM) strike price.

    Rounds the index price to the nearest strike interval for the given symbol.

    Args:
        index_price: Current index level (e.g. 24422.80).
        symbol: Index symbol (e.g. "NSE:NIFTY50-INDEX").

    Returns:
        ATM strike price as int (e.g. 24400 or 24450).
    """
    step = STRIKE_ROUNDING.get(symbol, 50)
    atm = round(index_price / step) * step
    return int(atm)


def build_option_symbol(
    index_symbol: str,
    strike: int,
    option_type: str,
    expiry_date: Optional[datetime] = None,
) -> str:
    """
    Construct a Fyers option symbol string.

    Uses monthly format: NSE:NIFTY{YY}{MON}{Strike}{CE/PE}
    Example: NSE:NIFTY26APR24400CE

    For backtesting, tries the trade month first, then the next month
    (since monthly options expire on last Thursday of the month, and
    the next month's options are already tradeable before that).

    Args:
        index_symbol: Index symbol (e.g. "NSE:NIFTY50-INDEX").
        strike: Strike price (e.g. 24400).
        option_type: "CE" for Call, "PE" for Put.
        expiry_date: Trade date. If None, uses the current date.

    Returns:
        Fyers option symbol string.
    """
    prefix = OPTION_PREFIX.get(index_symbol, "NSE:NIFTY")

    if expiry_date is None:
        expiry_date = get_ist_now()

    year_code = expiry_date.strftime("%y")  # "26"
    month_code = MONTH_CODES[expiry_date.month]  # "APR"

    symbol = f"{prefix}{year_code}{month_code}{strike}{option_type.upper()}"
    return symbol


def get_option_symbol_with_fallback(
    index_symbol: str,
    strike: int,
    option_type: str,
    trade_date: datetime,
    fyers=None,
    date_str: str = "",
) -> str:
    """
    Build option symbol, falling back to the next month if current month's
    options have expired (no historical data available from Fyers).

    Args:
        index_symbol: Index symbol.
        strike: ATM strike price.
        option_type: "CE" or "PE".
        trade_date: Date of the trade.
        fyers: Fyers API instance (for data availability check).
        date_str: Date string for API call.

    Returns:
        Valid Fyers option symbol string.
    """
    # Try current month first
    symbol = build_option_symbol(index_symbol, strike, option_type, trade_date)

    if fyers and date_str:
        # Check if data exists for current month's option
        data = {
            "symbol": symbol,
            "resolution": "5",
            "date_format": "1",
            "range_from": date_str,
            "range_to": date_str,
            "cont_flag": "1",
        }
        response = fyers.history(data=data)
        if response.get("s") == "ok" and response.get("candles"):
            return symbol

        # Fallback: try next month
        if trade_date.month == 12:
            next_month_date = trade_date.replace(year=trade_date.year + 1, month=1, day=1)
        else:
            next_month_date = trade_date.replace(month=trade_date.month + 1, day=1)

        next_symbol = build_option_symbol(index_symbol, strike, option_type, next_month_date)
        logger.info("Expired option %s, trying next month: %s", symbol, next_symbol)
        return next_symbol

    return symbol


def get_option_symbol_for_signal(
    index_symbol: str,
    index_price: float,
    direction: str,
    trade_date: Optional[datetime] = None,
) -> tuple[str, int]:
    """
    Get the correct option symbol for a trade signal.

    BUY signal on index → Buy ATM CE (Call)
    SELL signal on index → Buy ATM PE (Put)

    Args:
        index_symbol: Index symbol (e.g. "NSE:NIFTY50-INDEX").
        index_price: Current index price at signal time.
        direction: "BUY" or "SELL" from the strategy signal.
        trade_date: Date of the trade (for month code). Defaults to today.

    Returns:
        Tuple of (option_symbol, atm_strike).
    """
    atm_strike = get_atm_strike(index_price, index_symbol)
    option_type = "CE" if direction == "BUY" else "PE"
    option_symbol = build_option_symbol(
        index_symbol, atm_strike, option_type, trade_date,
    )

    logger.info(
        "Signal %s %s @ %.2f → Option: %s (ATM strike: %d)",
        direction, index_symbol, index_price, option_symbol, atm_strike,
    )
    return option_symbol, atm_strike


def get_option_lot_size(index_symbol: str) -> int:
    """
    Get the lot size for options on a given index.

    Args:
        index_symbol: Index symbol.

    Returns:
        Lot size (e.g. 75 for NIFTY, 30 for BANKNIFTY).
    """
    return OPTION_LOT_SIZES.get(index_symbol, 75)
