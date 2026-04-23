"""
Timezone and market-hours utilities for the Fyers Trading Bot.

All times are in IST (Asia/Kolkata). Provides helpers for checking market hours,
converting timestamps, and formatting times for display and logging.
"""

from datetime import datetime, time
from typing import Optional

import pytz

# ─── IST Timezone ────────────────────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")


def get_ist_now() -> datetime:
    """Return the current datetime in IST."""
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Convert a datetime to IST. If naive, assumes UTC."""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(IST)


def parse_time(time_str: str) -> time:
    """
    Parse a time string in HH:MM format to a datetime.time object.

    Args:
        time_str: Time string in "HH:MM" format (e.g. "09:15").

    Returns:
        datetime.time object.
    """
    return datetime.strptime(time_str, "%H:%M").time()


def is_market_open(
    market_open: str = "09:15",
    market_close: str = "15:20",
    now: Optional[datetime] = None,
) -> bool:
    """
    Check whether the current IST time is within market trading hours.

    Args:
        market_open: Market open time as "HH:MM" (default "09:15").
        market_close: Market close time as "HH:MM" (default "15:20").
        now: Optional datetime for testing; defaults to current IST time.

    Returns:
        True if current time is between market_open and market_close (inclusive start).
    """
    if now is None:
        now = get_ist_now()
    current_time = now.time()
    open_time = parse_time(market_open)
    close_time = parse_time(market_close)
    return open_time <= current_time <= close_time


def is_weekday(now: Optional[datetime] = None) -> bool:
    """
    Check if today is a weekday (Mon–Fri). Does NOT account for exchange holidays.

    Args:
        now: Optional datetime for testing; defaults to current IST time.

    Returns:
        True if the day is Monday (0) through Friday (4).
    """
    if now is None:
        now = get_ist_now()
    return now.weekday() < 5


def get_today_date_str(now: Optional[datetime] = None) -> str:
    """
    Return today's date as a YYYY-MM-DD string in IST.

    Args:
        now: Optional datetime for testing; defaults to current IST time.

    Returns:
        Date string like "2025-04-22".
    """
    if now is None:
        now = get_ist_now()
    return now.strftime("%Y-%m-%d")


def timestamp_to_ist(epoch: float) -> datetime:
    """
    Convert a Unix epoch timestamp (seconds) to an IST-aware datetime.

    Args:
        epoch: Unix timestamp in seconds.

    Returns:
        IST-aware datetime.
    """
    return datetime.fromtimestamp(epoch, tz=IST)


def format_ist(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Format a datetime as a string in IST.

    Args:
        dt: The datetime to format.
        fmt: strftime format string.

    Returns:
        Formatted datetime string.
    """
    return to_ist(dt).strftime(fmt)


def minutes_since_market_open(
    market_open: str = "09:15",
    now: Optional[datetime] = None,
) -> int:
    """
    Return the number of minutes elapsed since market open.

    Args:
        market_open: Market open time as "HH:MM" (default "09:15").
        now: Optional datetime for testing; defaults to current IST time.

    Returns:
        Integer minutes since market opened. Negative if before market open.
    """
    if now is None:
        now = get_ist_now()
    open_time = parse_time(market_open)
    open_dt = now.replace(hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0)
    delta = now - open_dt
    return int(delta.total_seconds() // 60)
