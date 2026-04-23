"""
Configuration module for the Fyers Trading Bot.

All user-configurable settings in one place. Sensitive values (API keys, tokens)
are loaded from a .env file using python-dotenv. All other settings are plain constants.
"""

import os
from dotenv import load_dotenv

# ─── Load environment variables from .env ───────────────────────────────────────
load_dotenv()

# ─── Fyers API Credentials ──────────────────────────────────────────────────────
FYERS_CLIENT_ID: str = os.getenv("FYERS_CLIENT_ID", "")
FYERS_SECRET_KEY: str = os.getenv("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI: str = os.getenv("FYERS_REDIRECT_URI", "")
FYERS_ACCESS_TOKEN: str = os.getenv("FYERS_ACCESS_TOKEN", "")

# ─── Telegram Credentials ───────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Trading Symbols ────────────────────────────────────────────────────────────
SYMBOLS: list[str] = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
]

# ─── Candle Settings ────────────────────────────────────────────────────────────
CANDLE_TIMEFRAME_MINUTES: int = 5

# ─── Active Strategies ──────────────────────────────────────────────────────────
ACTIVE_STRATEGIES: list[str] = ["range_breakout", "smc"]

# ─── Risk Management ────────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT: float = 1.0          # max 1% of capital per trade
MIN_REWARD_RISK_RATIO: float = 2.0       # only take trades with RR >= 1:2
MAX_DAILY_LOSS_INR: float = 5000.0       # hard stop for the day in rupees
MAX_TRADES_PER_DAY: int = 5              # stop after 5 trades regardless of PnL
ATR_PERIOD: int = 14
ATR_SL_MULTIPLIER: float = 1.5

# ─── Capital ─────────────────────────────────────────────────────────────────────
CAPITAL: float = 100_000.0               # total trading capital in INR

# ─── Market Hours (IST) ─────────────────────────────────────────────────────────
MARKET_OPEN: str = "09:15"
MARKET_CLOSE: str = "15:20"              # EOD square-off time
ENTRY_CUTOFF: str = "14:00"              # no new entries after 2 PM (theta decay)

# ─── Lot Sizes (Options) ─────────────────────────────────────────────────────────
LOT_SIZES: dict[str, int] = {
    "NSE:NIFTY50-INDEX": 65,
    "NSE:NIFTYBANK-INDEX": 30,
}

# ─── Option Trading Settings ────────────────────────────────────────────────────
# SL and Target are tracked on the OPTION PREMIUM, not on the index level.
# BUY signal → Buy ATM CE, SELL signal → Buy ATM PE
OPTION_SL_PCT: float = 15.0              # SL at 15% loss of entry premium
OPTION_TARGET_PCT: float = 20.0          # Target at 20% gain of entry premium

# ─── Trailing SL Settings ──────────────────────────────────────────────────────
TRAIL_ACTIVATION_PCT: float = 7.0        # start trailing after +7% gain
TRAIL_DISTANCE_PCT: float = 7.0          # trail 7% below peak premium

# ─── Signal Filters ────────────────────────────────────────────────────────────
MAX_POSITIONS_PER_SYMBOL: int = 1        # max 1 open position per underlying
SIGNAL_COOLDOWN_MINUTES: int = 15        # min gap between signals on same symbol

# ─── DRY RUN Mode ───────────────────────────────────────────────────────────────
# When True, orders are printed to console instead of placed via Fyers.
# Set to False ONLY when ready for live trading.
DRY_RUN: bool = True

# ─── Logging ─────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = "INFO"
LOG_FILE: str = "trading_bot.log"

# ─── Paths ───────────────────────────────────────────────────────────────────────
TOKEN_FILE: str = "token.json"
TRADES_CSV: str = "trades_log.csv"
TRADES_DB: str = os.path.join(os.path.dirname(__file__), "logger", "trades.db")
