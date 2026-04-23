"""
Fyers WebSocket client module for the Fyers Trading Bot.

Connects to the Fyers WebSocket v3 data feed, subscribes to symbol updates,
extracts tick data (LTP, volume, timestamp), and forwards ticks to the
CandleBuilder. Runs in a daemon thread. Auto-reconnects on disconnect.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from fyers_apiv3.FyersWebsocket import data_ws

from config import FYERS_CLIENT_ID, SYMBOLS
from notifications.telegram_alert import send_error
from utils.time_utils import get_ist_now

logger = logging.getLogger(__name__)

# Maximum reconnection attempts before giving up
MAX_RETRIES = 10
RECONNECT_DELAY_SECONDS = 5


class WebSocketClient:
    """
    Fyers WebSocket v3 client for real-time tick data.

    Subscribes to configured symbols and forwards tick data to a callback
    function. Runs in a daemon thread and auto-reconnects on disconnect.
    """

    def __init__(
        self,
        access_token: str,
        on_tick: Callable[[str, float, float, float], None],
    ) -> None:
        """
        Initialize the WebSocket client.

        Args:
            access_token: Valid Fyers access token.
            on_tick: Callback function(symbol, price, volume, timestamp)
                     called on each tick.
        """
        self._access_token = access_token
        self._on_tick = on_tick
        self._ws: Optional[data_ws.FyersDataSocket] = None
        self._thread: Optional[threading.Thread] = None
        self._retry_count = 0
        self._running = False
        self._connected = False   # True only while WS handshake is active
        self._last_tick_time: float = time.time()  # watchdog: last tick received
        self._symbols = SYMBOLS

        logger.info(
            "WebSocketClient initialized for %d symbols: %s",
            len(self._symbols),
            self._symbols,
        )

    def _on_message(self, message: Any) -> None:
        """
        Handle incoming WebSocket messages.

        Extracts symbol, LTP, volume, and timestamp from the message
        and forwards to the on_tick callback.

        Args:
            message: Raw message from the Fyers WebSocket.
        """
        try:
            if isinstance(message, list):
                for tick in message:
                    self._process_single_tick(tick)
            elif isinstance(message, dict):
                self._process_single_tick(message)
            else:
                logger.debug("Unknown message format: %s", type(message))
        except Exception as e:
            logger.error("Error processing tick message: %s — %s", e, message)

    def _process_single_tick(self, tick: dict) -> None:
        """
        Process a single tick dict and forward to callback.

        Args:
            tick: Dict with symbol data from Fyers WebSocket.
        """
        symbol = tick.get("symbol", "")
        ltp = tick.get("ltp", 0.0)
        volume = tick.get("vol_traded_today", 0.0)
        timestamp = tick.get("exch_feed_time", time.time())

        if symbol and ltp > 0 and self._on_tick:
            self._last_tick_time = time.time()  # watchdog update
            self._on_tick(symbol, ltp, volume, timestamp)

    def _on_connect(self) -> None:
        """Handle WebSocket connection event."""
        logger.info("WebSocket connected at %s", get_ist_now().strftime("%H:%M:%S"))
        self._retry_count = 0
        self._connected = True

        # Subscribe to all configured symbols
        data_type = "SymbolUpdate"
        if self._ws:
            self._ws.subscribe(symbols=self._symbols, data_type=data_type)
            logger.info(
                "Subscribed to %s for %d symbols",
                data_type,
                len(self._symbols),
            )

    def _on_error(self, error: Any) -> None:
        """
        Handle WebSocket error event.

        Logs the error and sends a Telegram alert.

        Args:
            error: The error object or message.
        """
        error_msg = f"WebSocket error: {error}"
        logger.error(error_msg)
        send_error(error_msg)

    def _on_close(self, *args: Any) -> None:
        """
        Handle WebSocket disconnect event — just mark as disconnected.
        The thread's retry loop handles reconnection.
        """
        logger.warning("WebSocket disconnected.")
        self._connected = False

    def _connect(self) -> None:
        """Create one WebSocket session and block until it closes or data freezes."""
        try:
            self._ws = data_ws.FyersDataSocket(
                access_token=f"{FYERS_CLIENT_ID}:{self._access_token}",
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=False,   # we handle reconnect ourselves
                on_connect=self._on_connect,
                on_close=self._on_close,
                on_error=self._on_error,
                on_message=self._on_message,
            )
            self._ws.connect()
            self._last_tick_time = time.time()

            # Watchdog: if no tick for 90s, treat as dead and reconnect
            WATCHDOG_SECONDS = 90
            while self._running and self._connected:
                time.sleep(5)
                stale = time.time() - self._last_tick_time
                if stale > WATCHDOG_SECONDS:
                    logger.warning("No tick for %.0fs — reconnecting", stale)
                    self._connected = False
                    break  # exit cleanly; _run_with_retry will create fresh WS
        except Exception as e:
            logger.error("WebSocket session error: %s", e)
            self._connected = False

    def _run_with_retry(self) -> None:
        """Thread target: connect and retry on failure indefinitely."""
        while self._running:
            logger.info("Starting WebSocket connection...")
            self._connect()
            if not self._running:
                break
            self._retry_count += 1
            if self._retry_count > MAX_RETRIES:
                msg = f"WebSocket failed after {MAX_RETRIES} retries. Manual restart needed."
                logger.critical(msg)
                send_error(msg)
                break
            delay = min(RECONNECT_DELAY_SECONDS * self._retry_count, 60)
            logger.info("Reconnecting in %ds (attempt %d)...", delay, self._retry_count)
            time.sleep(delay)
        logger.info("WebSocket thread exiting.")

    def start(self) -> None:
        """
        Start the WebSocket connection in a daemon thread with auto-retry.
        """
        if self._running:
            logger.warning("WebSocket is already running.")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_with_retry,
            name="FyersWebSocket",
            daemon=True,
        )
        self._thread.start()
        logger.info("WebSocket thread started.")

    def subscribe_option(self, option_symbol: str) -> None:
        """
        Dynamically subscribe to an option symbol for real-time LTP.

        Called after an option position is opened, so the WebSocket
        starts forwarding ticks for that option to the CandleBuilder/monitor.

        Args:
            option_symbol: Fyers option symbol (e.g. NSE:NIFTY26APR24400CE).
        """
        if not self._ws:
            logger.warning("WebSocket not connected — cannot subscribe to %s", option_symbol)
            return
        try:
            self._ws.subscribe(symbols=[option_symbol], data_type="SymbolUpdate")
            logger.info("Subscribed to option ticks: %s", option_symbol)
        except Exception as e:
            logger.error("Failed to subscribe to %s: %s", option_symbol, e)

    def unsubscribe_option(self, option_symbol: str) -> None:
        """
        Unsubscribe from an option symbol after position is closed.

        Args:
            option_symbol: Fyers option symbol to unsubscribe.
        """
        if not self._ws:
            return
        try:
            self._ws.unsubscribe(symbols=[option_symbol], data_type="SymbolUpdate")
            logger.info("Unsubscribed from option ticks: %s", option_symbol)
        except Exception as e:
            logger.warning("Failed to unsubscribe from %s: %s", option_symbol, e)

    def stop(self) -> None:
        """Stop the WebSocket connection and clean up."""
        self._running = False
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception as e:
                logger.warning("Error closing WebSocket: %s", e)
        logger.info("WebSocket stopped.")

    @property
    def is_running(self) -> bool:
        """Return whether the WebSocket is actively connected and receiving data."""
        return self._running and self._connected
