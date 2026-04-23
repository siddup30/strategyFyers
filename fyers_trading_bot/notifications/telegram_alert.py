"""
Telegram notification module for the Fyers Trading Bot.

Sends formatted messages to a Telegram chat via the Bot API using the
requests library. No external Telegram SDK needed.

Message types: trade entry, trade exit, daily summary, error alerts.
"""

import logging
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# ─── Telegram Bot API base URL ───────────────────────────────────────────────────
_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a message to the configured Telegram chat.

    Args:
        text: The message text (supports HTML formatting).
        parse_mode: Telegram parse mode ("HTML" or "Markdown").

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured. Skipping alert.")
        return False

    url = _BASE_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("ok"):
            logger.debug("Telegram message sent successfully.")
            return True
        else:
            logger.warning(
                "Telegram API error: %s — %s",
                response.status_code,
                response.text,
            )
            return False
    except requests.RequestException as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


def send_trade_entry(
    signal: dict,
    qty: int,
    lot_size: int,
) -> bool:
    """
    Send a trade entry alert.

    Args:
        signal: Signal dict with symbol, direction, entry_price, sl, target,
                strategy, reason, and optional confidence_score.
        qty: Total quantity to be traded.
        lot_size: Size of one lot for the symbol.

    Returns:
        True if the message was sent successfully.
    """
    direction = signal["direction"]
    emoji = "🟢" if direction == "BUY" else "🔴"
    lots = qty // lot_size if lot_size > 0 else qty

    entry = signal["entry_price"]
    sl = signal["sl"]
    target = signal["target"]

    # Calculate reward-risk ratio
    risk = abs(entry - sl)
    reward = abs(target - entry)
    rr = f"1:{reward / risk:.1f}" if risk > 0 else "N/A"

    confidence = signal.get("confidence_score", "N/A")

    text = (
        f"{emoji} <b>{direction} SIGNAL — {signal['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry: <code>{entry:,.2f}</code>\n"
        f"🛡️ SL: <code>{sl:,.2f}</code>\n"
        f"🎯 Target: <code>{target:,.2f}</code>\n"
        f"📦 Qty: {qty} ({lots} lot{'s' if lots != 1 else ''})\n"
        f"⚖️ RR: {rr}\n"
        f"📊 Strategy: {signal['strategy']}\n"
        f"🔍 Confidence: {confidence}\n"
        f"💡 Reason: {signal.get('reason', 'N/A')}"
    )
    return _send_message(text)


def send_trade_exit(
    symbol: str,
    exit_price: float,
    pnl: float,
    reason: str,
) -> bool:
    """
    Send a trade exit alert.

    Args:
        symbol: Trading symbol.
        exit_price: Price at which the position was closed.
        pnl: Profit/Loss in INR.
        reason: Exit reason — "target_hit", "sl_hit", "trailing_sl", "eod_squareoff".

    Returns:
        True if the message was sent successfully.
    """
    emoji = "✅" if pnl >= 0 else "❌"
    reason_labels = {
        "target_hit": "🎯 Target Hit",
        "sl_hit": "🛑 Stop Loss Hit",
        "trailing_sl": "🔄 Trailing SL Hit",
        "eod_squareoff": "🕐 EOD Square-off",
    }
    reason_text = reason_labels.get(reason, reason)

    text = (
        f"{emoji} <b>TRADE CLOSED — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Exit Price: <code>{exit_price:,.2f}</code>\n"
        f"{'📈' if pnl >= 0 else '📉'} PnL: <code>₹{pnl:+,.2f}</code>\n"
        f"📋 Reason: {reason_text}"
    )
    return _send_message(text)


def send_daily_summary(
    trades_today: int,
    total_pnl: float,
    winners: int = 0,
    losers: int = 0,
) -> bool:
    """
    Send end-of-day trading summary.

    Args:
        trades_today: Total number of trades taken today.
        total_pnl: Net PnL for the day in INR.
        winners: Number of winning trades.
        losers: Number of losing trades.

    Returns:
        True if the message was sent successfully.
    """
    emoji = "🏆" if total_pnl >= 0 else "📉"
    win_rate = (winners / trades_today * 100) if trades_today > 0 else 0

    text = (
        f"{emoji} <b>DAILY SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total Trades: {trades_today}\n"
        f"✅ Winners: {winners}\n"
        f"❌ Losers: {losers}\n"
        f"📈 Win Rate: {win_rate:.1f}%\n"
        f"💰 Net PnL: <code>₹{total_pnl:+,.2f}</code>"
    )
    return _send_message(text)


def send_error(error_message: str) -> bool:
    """
    Send an error alert to Telegram.

    Args:
        error_message: The error description.

    Returns:
        True if the message was sent successfully.
    """
    text = (
        f"🚨 <b>BOT ERROR</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{error_message[:3000]}</code>"
    )
    return _send_message(text)
