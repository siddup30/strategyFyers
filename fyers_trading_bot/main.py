"""
Fyers Algorithmic Trading Bot — Main Entry Point.

Orchestrates the complete trading pipeline:
1. Authenticates with Fyers API
2. Initializes all components (CandleBuilder, StrategyManager, RiskManager,
   OrderManager, TradeLogger)
3. Starts WebSocket feed in a background thread
4. Starts position monitoring in a second background thread
5. Schedules EOD square-off and daily summary via APScheduler
6. Runs a heartbeat loop showing system status every 60 seconds
"""

import logging
import os
import signal
import sys
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler

# ─── Setup Python path ──────────────────────────────────────────────────────────
# Ensure the project root is on the path for absolute imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    DRY_RUN,
    LOG_FILE,
    LOG_LEVEL,
    MARKET_CLOSE,
    SYMBOLS,
    TRADES_CSV,
    TRADES_DB,
)
from auth.fyers_auth import load_or_refresh_token
from data.candle_builder import CandleBuilder
from data.websocket_client import WebSocketClient
from execution.order_manager import OrderManager
from logger.trade_logger import TradeLogger
from notifications.telegram_alert import send_daily_summary, send_error
from risk.risk_manager import RiskManager
from strategies.strategy_manager import StrategyManager
from utils.time_utils import get_ist_now, is_market_open
from utils.option_utils import get_atm_strike, get_option_symbol_with_fallback

# ─── Logging Setup ──────────────────────────────────────────────────────────────
def setup_logging() -> None:
    """Configure logging with both file and console handlers."""
    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
    )
    date_format = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(LOG_FILE, mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root_logger.addHandler(file_handler)


logger = logging.getLogger("main")


# ─── Global component references (for signal handler) ───────────────────────────
_order_manager: OrderManager | None = None
_trade_logger: TradeLogger | None = None
_risk_manager: RiskManager | None = None
_ws_client: WebSocketClient | None = None
_scheduler: BackgroundScheduler | None = None


def handle_signal_event(
    signal_dict: dict,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    fyers,
) -> None:
    """
    Process a strategy signal through the risk → option order pipeline.

    Args:
        signal_dict: Signal from a strategy (index-level).
        risk_manager: RiskManager instance.
        order_manager: OrderManager instance.
        fyers: Fyers API instance (for option quote lookup).
    """
    index_symbol = signal_dict["symbol"]
    direction = signal_dict["direction"]

    logger.info(
        "Processing signal: %s %s @ %.2f (strategy: %s)",
        direction, index_symbol, signal_dict["entry_price"], signal_dict["strategy"],
    )

    # Validate through risk manager (with current open positions for stacking check)
    open_positions = order_manager.get_open_positions()
    is_valid, reason = risk_manager.validate_signal(signal_dict, open_positions=open_positions)
    if not is_valid:
        logger.info("Signal rejected by risk manager: %s", reason)
        return

    # ── Resolve ATM option symbol ──
    trade_date = get_ist_now()
    date_str = trade_date.strftime("%Y-%m-%d")
    atm_strike = get_atm_strike(signal_dict["entry_price"], index_symbol)
    option_type = "CE" if direction == "BUY" else "PE"
    option_symbol = get_option_symbol_with_fallback(
        index_symbol, atm_strike, option_type, trade_date,
        fyers=fyers, date_str=date_str,
    )

    # ── Fetch current option premium via REST quote ──
    entry_premium = None
    try:
        data = {"symbols": option_symbol}
        response = fyers.quotes(data=data)
        if response.get("s") == "ok" and response.get("d"):
            entry_premium = float(response["d"][0].get("v", {}).get("lp", 0) or 0)
    except Exception as e:
        logger.error("Failed to fetch option premium for %s: %s", option_symbol, e)

    if not entry_premium or entry_premium <= 0:
        logger.warning("No valid premium for %s — skipping signal", option_symbol)
        return

    # ── Calculate lot size based on option premium ──
    qty_lots = risk_manager.calculate_lot_size(index_symbol, entry_premium)
    from config import LOT_SIZES
    lot_size = LOT_SIZES.get(index_symbol, 65)
    qty = qty_lots * lot_size

    # ── Place option order ──
    pos_id = order_manager.place_option_order(signal_dict, option_symbol, entry_premium, qty)
    if pos_id:
        logger.info(
            "Option order placed — %s | pos: %s | premium: ₹%.2f | qty: %d",
            option_symbol, pos_id, entry_premium, qty,
        )
    else:
        logger.warning("Option order placement failed for %s %s", direction, index_symbol)


def on_candle_close_handler(
    symbol: str,
    candle: dict,
    candle_builder: CandleBuilder,
    strategy_manager: StrategyManager,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    fyers,
) -> None:
    """
    Handle a completed index candle: run strategies and process option signals.

    Args:
        symbol: Index trading symbol.
        candle: Completed OHLCV candle dict.
        candle_builder: CandleBuilder instance (for history).
        strategy_manager: StrategyManager instance.
        risk_manager: RiskManager instance.
        order_manager: OrderManager instance.
        fyers: Fyers API instance (for option symbol/premium lookup).
    """
    df = candle_builder.get_history(symbol)
    if df.empty:
        return

    strategy_manager.on_candle_close(symbol, candle)
    signals = strategy_manager.evaluate(symbol, df)

    for signal_dict in signals:
        handle_signal_event(signal_dict, risk_manager, order_manager, fyers)


def eod_square_off() -> None:
    """Square off all positions at end of day."""
    global _order_manager
    logger.info("═══ EOD SQUARE-OFF TRIGGERED ═══")
    if _order_manager:
        _order_manager.square_off_all()


def eod_daily_summary() -> None:
    """Send the daily trading summary via Telegram."""
    global _trade_logger
    logger.info("═══ DAILY SUMMARY ═══")
    if _trade_logger:
        summary = _trade_logger.get_daily_summary()
        send_daily_summary(
            trades_today=summary["total_trades"],
            total_pnl=summary["total_pnl"],
            winners=summary["winners"],
            losers=summary["losers"],
        )
        logger.info(
            "Daily summary: %d trades, PnL: ₹%.2f (W:%d L:%d)",
            summary["total_trades"],
            summary["total_pnl"],
            summary["winners"],
            summary["losers"],
        )


def graceful_shutdown(signum=None, frame=None) -> None:
    """Handle shutdown: square off, send summary, clean up."""
    global _order_manager, _ws_client, _scheduler

    logger.info("═══ GRACEFUL SHUTDOWN INITIATED ═══")

    # Square off positions
    if _order_manager:
        _order_manager.square_off_all()
        _order_manager.stop_monitoring()

    # Send daily summary
    eod_daily_summary()

    # Stop WebSocket
    if _ws_client:
        _ws_client.stop()

    # Stop scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)

    logger.info("Shutdown complete. Goodbye!")
    sys.exit(0)


def main() -> None:
    """Main entry point — starts the trading bot."""
    global _order_manager, _trade_logger, _risk_manager, _ws_client, _scheduler

    setup_logging()

    logger.info("═" * 60)
    logger.info("  FYERS ALGORITHMIC TRADING BOT")
    logger.info("  DRY_RUN: %s", DRY_RUN)
    logger.info("  Symbols: %s", SYMBOLS)
    logger.info("═" * 60)

    if DRY_RUN:
        print("\n⚠️  DRY RUN MODE — orders will be printed, not placed.\n")

    # ─── Step 1: Authenticate ─────────────────────────────────────────────
    try:
        access_token = load_or_refresh_token()
    except Exception as e:
        logger.critical("Authentication failed: %s", e)
        send_error(f"Bot failed to start — auth error: {e}")
        sys.exit(1)

    # ─── Step 2: Initialize components ────────────────────────────────────
    logger.info("Initializing components...")

    from fyers_apiv3 import fyersModel
    from config import FYERS_CLIENT_ID
    fyers = fyersModel.FyersModel(
        client_id=FYERS_CLIENT_ID,
        is_async=False,
        token=access_token,
        log_path="",
    )

    _risk_manager = RiskManager()
    _trade_logger = TradeLogger(db_path=TRADES_DB, csv_path=TRADES_CSV)

    strategy_manager = StrategyManager()

    # CandleBuilder defined before WebSocket so tick handler can reference it
    candle_builder = CandleBuilder(
        on_candle_close=None,  # will be set below
    )

    # Deferred to avoid circular reference — set after order_manager is ready
    _order_manager = OrderManager(
        access_token=access_token,
        risk_manager=_risk_manager,
        trade_logger=_trade_logger,
        ws_subscribe_fn=None,     # patched after ws_client created
        ws_unsubscribe_fn=None,
    )

    def _on_tick_handler(symbol: str, ltp: float, volume: float, timestamp: float):
        """Route ticks: index → candle builder; options → order manager LTP cache."""
        if symbol.endswith("CE") or symbol.endswith("PE"):
            _order_manager.on_option_tick(symbol, ltp)
        else:
            candle_builder.process_tick(symbol, ltp, volume, timestamp)

    # WebSocket with the complete tick handler
    _ws_client = WebSocketClient(
        access_token=access_token,
        on_tick=_on_tick_handler,
    )

    # Now wire back subscribe fns and candle close handler
    _order_manager._ws_subscribe = _ws_client.subscribe_option
    _order_manager._ws_unsubscribe = _ws_client.unsubscribe_option

    candle_builder._on_candle_close = lambda symbol, candle: on_candle_close_handler(
        symbol, candle, candle_builder, strategy_manager,
        _risk_manager, _order_manager, fyers,
    )

    # ─── Step 3: Start WebSocket ──────────────────────────────────────────
    _ws_client.start()
    logger.info("WebSocket feed started (index + option ticks).")

    # ─── Step 4: Start position monitoring thread ─────────────────────────
    monitor_thread = threading.Thread(
        target=_order_manager.monitor_positions,
        name="PositionMonitor",
        daemon=True,
    )
    monitor_thread.start()
    logger.info("Position monitor thread started.")

    # ─── Step 5: Schedule EOD tasks ───────────────────────────────────────
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    # Parse market close time for scheduling
    close_parts = MARKET_CLOSE.split(":")
    close_hour = int(close_parts[0])
    close_minute = int(close_parts[1])

    _scheduler.add_job(
        eod_square_off,
        "cron",
        hour=close_hour,
        minute=close_minute,
        day_of_week="mon-fri",
        id="eod_squareoff",
    )

    _scheduler.add_job(
        eod_daily_summary,
        "cron",
        hour=15,
        minute=35,
        day_of_week="mon-fri",
        id="daily_summary",
    )

    _scheduler.start()
    logger.info(
        "Scheduler started — square-off at %s, summary at 15:35",
        MARKET_CLOSE,
    )

    # ─── Step 6: Register signal handlers ─────────────────────────────────
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    # ─── Step 7: Heartbeat loop ───────────────────────────────────────────
    logger.info("Bot is running. Press Ctrl+C to stop.\n")

    try:
        while True:
            now = get_ist_now()
            open_count = _order_manager.open_position_count
            daily_pnl = _risk_manager.daily_pnl
            trade_count = _risk_manager.trade_count_today
            market_status = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"

            heartbeat = (
                f"💓 {now.strftime('%H:%M:%S')} IST | "
                f"Market: {market_status} | "
                f"Positions: {open_count} | "
                f"Trades: {trade_count} | "
                f"PnL: ₹{daily_pnl:+,.2f} | "
                f"WS: {'✅' if _ws_client.is_running else '❌'}"
            )
            print(heartbeat)
            logger.debug(heartbeat)

            time.sleep(60)

    except KeyboardInterrupt:
        graceful_shutdown()


if __name__ == "__main__":
    main()
