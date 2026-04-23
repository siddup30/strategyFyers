"""
Order Management module for the Fyers Trading Bot.

Places, tracks, and cancels Fyers option orders. Supports:
- ATM CE/PE market order entry based on index signal direction
- Premium-based SL and target (% of entry premium)
- Progressive trailing SL on option premium
- Real-time LTP via WebSocket tick updates
- End-of-day square-off
- DRY_RUN mode for paper trading
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from fyers_apiv3 import fyersModel

from config import (
    DRY_RUN,
    FYERS_CLIENT_ID,
    LOT_SIZES,
    OPTION_SL_PCT,
    OPTION_TARGET_PCT,
    TRAIL_ACTIVATION_PCT,
    TRAIL_DISTANCE_PCT,
)
from logger.trade_logger import TradeLogger
from notifications.telegram_alert import (
    send_error,
    send_trade_entry,
    send_trade_exit,
)
from risk.risk_manager import RiskManager
from utils.time_utils import get_ist_now

logger = logging.getLogger(__name__)


class OrderManager:
    """
    Manages option order placement, position tracking, and lifecycle management.

    Flow per signal:
      1. Convert index signal → ATM option symbol (CE for BUY, PE for SELL)
      2. Fetch current option premium via REST quote
      3. Calculate SL / Target as % of entry premium
      4. Place MARKET order for the option
      5. Monitor LTP via WebSocket ticks with progressive trailing SL
      6. Exit when SL/Target hit or EOD

    In DRY_RUN mode, orders are printed to console instead of placed.
    """

    def __init__(
        self,
        access_token: str,
        risk_manager: RiskManager,
        trade_logger: TradeLogger,
        ws_subscribe_fn: Optional[Callable[[str], None]] = None,
        ws_unsubscribe_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Initialize the OrderManager.

        Args:
            access_token: Valid Fyers access token.
            risk_manager: RiskManager instance for recording trade results.
            trade_logger: TradeLogger instance for logging trades.
            ws_subscribe_fn: Callback to subscribe to option WebSocket ticks.
            ws_unsubscribe_fn: Callback to unsubscribe from option WebSocket ticks.
        """
        self._fyers = fyersModel.FyersModel(
            client_id=FYERS_CLIENT_ID,
            is_async=False,
            token=access_token,
            log_path="",
        )
        self._risk_manager = risk_manager
        self._trade_logger = trade_logger
        self._ws_subscribe = ws_subscribe_fn
        self._ws_unsubscribe = ws_unsubscribe_fn
        self._lock = threading.Lock()
        self._monitoring = False

        # {internal_id: position_dict}
        self._positions: dict[str, dict] = {}
        self._next_id = 1

        # option_symbol -> latest LTP (updated via WebSocket)
        self._option_ltp: dict[str, float] = {}

        logger.info("OrderManager initialized (DRY_RUN=%s)", DRY_RUN)

    # ─────────────────────────────────────────────────────────────────────────────
    # Public: called by main.py after signal fires
    # ─────────────────────────────────────────────────────────────────────────────

    def place_option_order(
        self,
        signal: dict,
        option_symbol: str,
        entry_premium: float,
        qty: int,
    ) -> Optional[str]:
        """
        Place an ATM option entry order based on a validated index signal.

        Args:
            signal: Validated signal dict from a strategy (index-level).
            option_symbol: Fyers option symbol (e.g. NSE:NIFTY26APR24400CE).
            entry_premium: Current option premium at signal time.
            qty: Total quantity (lots × lot_size).

        Returns:
            Internal position ID string, or None if order failed.
        """
        direction = signal["direction"]
        index_symbol = signal["symbol"]
        lot_size = LOT_SIZES.get(index_symbol, 65)

        # Calculate SL and target on the OPTION PREMIUM
        sl_premium = round(entry_premium * (1 - OPTION_SL_PCT / 100), 2)
        target_premium = round(entry_premium * (1 + OPTION_TARGET_PCT / 100), 2)

        # Option side is always BUY (we buy CE or PE)
        order_data = {
            "symbol": option_symbol,
            "qty": qty,
            "type": 2,       # MARKET
            "side": 1,       # BUY
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }

        if DRY_RUN:
            logger.info(
                "[DRY RUN] OPTION ORDER: BUY %s %d qty @ ~%.2f | SL: %.2f | T: %.2f",
                option_symbol, qty, entry_premium, sl_premium, target_premium,
            )
            print(
                f"\n{'='*55}\n"
                f"  🔔 [DRY RUN] {direction} SIGNAL → OPTION ORDER\n"
                f"  Index Signal: {index_symbol} {direction}\n"
                f"  Option:  {option_symbol}\n"
                f"  Qty:     {qty} ({qty // lot_size} lot{'s' if qty // lot_size != 1 else ''})\n"
                f"  Premium: ₹{entry_premium:,.2f}\n"
                f"  SL:      ₹{sl_premium:,.2f} (-{OPTION_SL_PCT:.0f}%)\n"
                f"  Target:  ₹{target_premium:,.2f} (+{OPTION_TARGET_PCT:.0f}%)\n"
                f"  Strategy: {signal['strategy']}\n"
                f"{'='*55}\n"
            )
            internal_id = self._register_position(
                index_symbol=index_symbol,
                option_symbol=option_symbol,
                direction=direction,
                qty=qty,
                entry_premium=entry_premium,
                sl_premium=sl_premium,
                target_premium=target_premium,
                signal=signal,
            )
            return internal_id

        # ─── Live order placement ─────────────────────────────────────────
        try:
            response = self._fyers.place_order(data=order_data)
            logger.info("Option entry order response: %s", response)

            if response.get("s") != "ok":
                error_msg = (
                    f"Option entry order failed for {option_symbol}: "
                    f"{response.get('message', 'Unknown error')}"
                )
                logger.error(error_msg)
                send_error(error_msg)
                return None

            order_id = response.get("id", "")
            internal_id = self._register_position(
                index_symbol=index_symbol,
                option_symbol=option_symbol,
                direction=direction,
                qty=qty,
                entry_premium=entry_premium,
                sl_premium=sl_premium,
                target_premium=target_premium,
                signal=signal,
                fyers_order_id=order_id,
            )
            return internal_id

        except Exception as e:
            error_msg = f"Option order placement exception for {option_symbol}: {e}"
            logger.error(error_msg, exc_info=True)
            send_error(error_msg)
            return None

    def on_option_tick(self, option_symbol: str, ltp: float) -> None:
        """
        Called by WebSocket tick handler when an option LTP update arrives.

        Updates internal LTP cache so position monitor can use it.

        Args:
            option_symbol: The option symbol that received a tick.
            ltp: Latest traded price of the option.
        """
        with self._lock:
            self._option_ltp[option_symbol] = ltp

    # ─────────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────────

    def _register_position(
        self,
        index_symbol: str,
        option_symbol: str,
        direction: str,
        qty: int,
        entry_premium: float,
        sl_premium: float,
        target_premium: float,
        signal: dict,
        fyers_order_id: str = "",
    ) -> str:
        """Register a new option position in the internal tracker."""
        with self._lock:
            internal_id = f"POS_{self._next_id:04d}"
            self._next_id += 1

            lot_size = LOT_SIZES.get(index_symbol, 65)

            self._positions[internal_id] = {
                "index_symbol": index_symbol,
                "option_symbol": option_symbol,
                "direction": direction,
                "qty": qty,
                "lot_size": lot_size,
                "entry_premium": entry_premium,
                "sl_premium": sl_premium,
                "target_premium": target_premium,
                "peak_premium": entry_premium,        # track for trailing SL
                "trailing_stage": 0,                  # 0=none, 1=active
                "trailing_activated": False,
                "fyers_order_id": fyers_order_id,
                "signal": signal,
                "trade_id": None,
            }

            # Log entry
            trade_id = self._trade_logger.log_entry(signal, qty)
            self._positions[internal_id]["trade_id"] = trade_id

            # Telegram alert
            send_trade_entry(signal, qty, lot_size)

            # Subscribe to option ticks via WebSocket
            if self._ws_subscribe:
                self._ws_subscribe(option_symbol)

        logger.info(
            "Option position %s registered: %s %s %d @ ₹%.2f | SL: ₹%.2f | T: ₹%.2f",
            internal_id, direction, option_symbol, qty,
            entry_premium, sl_premium, target_premium,
        )
        return internal_id

    # ─────────────────────────────────────────────────────────────────────────────
    # Position monitoring (runs in daemon thread)
    # ─────────────────────────────────────────────────────────────────────────────

    def monitor_positions(self) -> None:
        """
        Continuously monitor open option positions for SL/target hits.

        Uses LTP from WebSocket tick cache (updated via on_option_tick).
        Falls back to Fyers quote API if WebSocket data is stale.
        Runs in a daemon thread every 5 seconds.
        """
        self._monitoring = True
        logger.info("Position monitoring started.")

        while self._monitoring:
            try:
                with self._lock:
                    position_ids = list(self._positions.keys())

                for pos_id in position_ids:
                    self._check_option_position(pos_id)

            except Exception as e:
                logger.error("Error in position monitoring loop: %s", e)
                send_error(f"Position monitor error: {e}")

            time.sleep(5)  # tighter loop since options move fast

        logger.info("Position monitoring stopped.")

    def _check_option_position(self, pos_id: str) -> None:
        """
        Check a single option position for SL/target and apply trailing SL.

        Progressive trailing SL logic (mirrors backtest):
          - Once premium gains >= TRAIL_ACTIVATION_PCT, activate trailing
          - Trail SL = peak_premium × (1 - TRAIL_DISTANCE_PCT / 100)
          - SL ratchets up as peak rises, never goes back down
        """
        with self._lock:
            pos = self._positions.get(pos_id)
            if pos is None:
                return
            option_symbol = pos["option_symbol"]
            entry = pos["entry_premium"]
            sl = pos["sl_premium"]
            target = pos["target_premium"]
            peak = pos["peak_premium"]
            trailing = pos["trailing_activated"]

        # Get LTP — prefer WebSocket cache, fall back to REST
        ltp = self._get_option_ltp(option_symbol)
        if ltp is None or ltp <= 0:
            return

        with self._lock:
            if pos_id not in self._positions:
                return
            # Update peak
            if ltp > self._positions[pos_id]["peak_premium"]:
                self._positions[pos_id]["peak_premium"] = ltp
            peak = self._positions[pos_id]["peak_premium"]

        gain_pct = (peak - entry) / entry * 100

        # ─── Progressive trailing SL ──────────────────────────────────────
        if gain_pct >= TRAIL_ACTIVATION_PCT:
            new_trail_sl = round(peak * (1 - TRAIL_DISTANCE_PCT / 100), 2)
            with self._lock:
                if pos_id in self._positions:
                    current_sl = self._positions[pos_id]["sl_premium"]
                    if new_trail_sl > current_sl:
                        self._positions[pos_id]["sl_premium"] = new_trail_sl
                        self._positions[pos_id]["trailing_activated"] = True
                        sl = new_trail_sl
                        locked_pct = (new_trail_sl - entry) / entry * 100
                        logger.info(
                            "Trailing SL updated for %s: peak=₹%.2f (+%.1f%%), "
                            "new SL=₹%.2f (locked %.1f%%)",
                            pos_id, peak, gain_pct, new_trail_sl, locked_pct,
                        )
                    trailing = self._positions[pos_id]["trailing_activated"]

        # ─── Check target hit ─────────────────────────────────────────────
        if ltp >= target:
            self._close_option_position(pos_id, ltp, "target_hit")
            return

        # ─── Check SL hit ─────────────────────────────────────────────────
        if ltp <= sl:
            reason = "trailing_sl" if trailing else "sl_hit"
            self._close_option_position(pos_id, ltp, reason)

    def _close_option_position(
        self,
        pos_id: str,
        exit_premium: float,
        reason: str,
    ) -> None:
        """Close an option position by placing a MARKET sell order."""
        with self._lock:
            pos = self._positions.pop(pos_id, None)
            if pos is None:
                return

        option_symbol = pos["option_symbol"]
        qty = pos["qty"]
        entry = pos["entry_premium"]
        lot_size = pos["lot_size"]
        lots = qty // lot_size

        # PnL = (exit - entry) × qty (we always bought the option)
        pnl = (exit_premium - entry) * qty

        if DRY_RUN:
            locked_pct = (exit_premium - entry) / entry * 100
            logger.info(
                "[DRY RUN] CLOSE %s: %s %d qty @ ₹%.2f | PnL: ₹%.2f | Reason: %s",
                pos_id, option_symbol, qty, exit_premium, pnl, reason,
            )
            print(
                f"\n{'='*55}\n"
                f"  {'✅' if pnl >= 0 else '❌'} [DRY RUN] OPTION POSITION CLOSED\n"
                f"  {pos_id}: {option_symbol}\n"
                f"  Exit Premium: ₹{exit_premium:,.2f} ({locked_pct:+.1f}%)\n"
                f"  PnL: ₹{pnl:+,.2f}\n"
                f"  Qty: {qty} ({lots} lot{'s' if lots != 1 else ''})\n"
                f"  Reason: {reason}\n"
                f"{'='*55}\n"
            )
        else:
            # Sell the option at market
            exit_order = {
                "symbol": option_symbol,
                "qty": qty,
                "type": 2,       # MARKET
                "side": -1,      # SELL
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            }
            try:
                response = self._fyers.place_order(data=exit_order)
                logger.info("Option exit order response for %s: %s", pos_id, response)
            except Exception as e:
                logger.error("Option exit order failed for %s: %s", pos_id, e)
                send_error(f"Option exit order failed for {pos_id}: {e}")

        # Unsubscribe from option ticks
        if self._ws_unsubscribe:
            self._ws_unsubscribe(option_symbol)

        # Clean up LTP cache
        with self._lock:
            self._option_ltp.pop(option_symbol, None)

        # Log exit and record PnL
        trade_id = pos.get("trade_id")
        if trade_id:
            self._trade_logger.log_exit(trade_id, exit_premium, pnl, reason)

        self._risk_manager.record_trade_result(pnl)
        send_trade_exit(option_symbol, exit_premium, pnl, reason)

    def _get_option_ltp(self, option_symbol: str) -> Optional[float]:
        """
        Get LTP for an option. Uses WebSocket cache first, then REST API.

        Args:
            option_symbol: Fyers option symbol.

        Returns:
            LTP as float, or None if unavailable.
        """
        # Try WebSocket cache first (preferred — real-time)
        with self._lock:
            ltp = self._option_ltp.get(option_symbol)
        if ltp and ltp > 0:
            return ltp

        # Fallback: REST quote (slower, ~1s latency)
        if DRY_RUN:
            return None

        try:
            data = {"symbols": option_symbol}
            response = self._fyers.quotes(data=data)
            if response.get("s") == "ok" and response.get("d"):
                ltp = response["d"][0].get("v", {}).get("lp", None)
                return float(ltp) if ltp else None
            return None
        except Exception as e:
            logger.error("Error fetching option LTP for %s: %s", option_symbol, e)
            return None

    # ─────────────────────────────────────────────────────────────────────────────
    # EOD / Lifecycle
    # ─────────────────────────────────────────────────────────────────────────────

    def square_off_all(self) -> None:
        """Close all open option positions at market price (EOD square-off)."""
        logger.info("Squaring off all open option positions...")

        with self._lock:
            position_ids = list(self._positions.keys())

        if not position_ids:
            logger.info("No open positions to square off.")
            return

        for pos_id in position_ids:
            with self._lock:
                pos = self._positions.get(pos_id)
            if pos is None:
                continue
            ltp = self._get_option_ltp(pos["option_symbol"])
            exit_price = ltp if ltp else pos["entry_premium"]
            self._close_option_position(pos_id, exit_price, "eod_squareoff")

        logger.info("All option positions squared off.")

    def stop_monitoring(self) -> None:
        """Stop the position monitoring loop."""
        self._monitoring = False
        logger.info("Position monitoring stop requested.")

    def get_open_positions(self) -> list[dict]:
        """Return all currently open option positions."""
        with self._lock:
            return [
                {
                    "id": pos_id,
                    "index_symbol": pos["index_symbol"],
                    "option_symbol": pos["option_symbol"],
                    "direction": pos["direction"],
                    "qty": pos["qty"],
                    "entry_premium": pos["entry_premium"],
                    "sl_premium": pos["sl_premium"],
                    "target_premium": pos["target_premium"],
                    "peak_premium": pos["peak_premium"],
                    "trailing_activated": pos["trailing_activated"],
                }
                for pos_id, pos in self._positions.items()
            ]

    @property
    def open_position_count(self) -> int:
        """Return the number of open option positions."""
        with self._lock:
            return len(self._positions)
