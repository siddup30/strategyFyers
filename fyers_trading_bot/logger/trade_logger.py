"""
Trade logging module for the Fyers Trading Bot.

Logs every trade to both a CSV file and an SQLite database.
Provides methods for logging entries, exits, and retrieving daily summaries.
"""

import csv
import logging
import os
import sqlite3
import threading
from typing import Optional

from utils.time_utils import get_ist_now, format_ist

logger = logging.getLogger(__name__)


class TradeLogger:
    """
    Dual-destination trade logger (CSV + SQLite).

    Thread-safe: all DB and CSV writes are protected by a threading.Lock.
    The SQLite database and CSV file are auto-created on first use.
    """

    def __init__(self, db_path: str, csv_path: str) -> None:
        """
        Initialize the TradeLogger.

        Args:
            db_path: Absolute path to the SQLite database file.
            csv_path: Path to the CSV log file.
        """
        self._db_path = db_path
        self._csv_path = csv_path
        self._lock = threading.Lock()
        self._init_db()
        self._init_csv()
        logger.info("TradeLogger initialized — DB: %s, CSV: %s", db_path, csv_path)

    def _init_db(self) -> None:
        """Create the trades table in SQLite if it does not exist."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    strategy        TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    entry_price     REAL NOT NULL,
                    sl              REAL NOT NULL,
                    target          REAL NOT NULL,
                    exit_price      REAL,
                    qty             INTEGER NOT NULL,
                    pnl             REAL,
                    exit_reason     TEXT,
                    rr_ratio        REAL,
                    confidence_score REAL,
                    status          TEXT DEFAULT 'OPEN'
                )
            """)
            conn.commit()

    def _init_csv(self) -> None:
        """Create the CSV file with headers if it does not exist."""
        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "id", "timestamp", "symbol", "strategy", "direction",
                    "entry_price", "sl", "target", "exit_price", "qty",
                    "pnl", "exit_reason", "rr_ratio", "confidence_score",
                ])

    def log_entry(self, signal: dict, qty: int) -> int:
        """
        Log a trade entry to both CSV and SQLite.

        Args:
            signal: Signal dict with keys: symbol, direction, entry_price,
                    sl, target, strategy, reason, and optional confidence_score.
            qty: Number of units traded.

        Returns:
            The trade ID (SQLite row id) for tracking the trade.
        """
        now = format_ist(get_ist_now())
        entry = signal["entry_price"]
        sl = signal["sl"]
        target = signal["target"]
        risk = abs(entry - sl)
        reward = abs(target - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        confidence = signal.get("confidence_score", 0.0)

        with self._lock:
            # Insert into SQLite
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO trades
                        (timestamp, symbol, strategy, direction, entry_price,
                         sl, target, qty, rr_ratio, confidence_score, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                    """,
                    (
                        now,
                        signal["symbol"],
                        signal["strategy"],
                        signal["direction"],
                        entry,
                        sl,
                        target,
                        qty,
                        rr_ratio,
                        confidence,
                    ),
                )
                trade_id = cursor.lastrowid
                conn.commit()

            # Append to CSV
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade_id, now, signal["symbol"], signal["strategy"],
                    signal["direction"], entry, sl, target, "",
                    qty, "", "", rr_ratio, confidence,
                ])

        logger.info(
            "Trade #%d ENTRY logged: %s %s @ %.2f",
            trade_id, signal["direction"], signal["symbol"], entry,
        )
        return trade_id

    def log_exit(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        """
        Log a trade exit, updating both the SQLite row and appending to CSV.

        Args:
            trade_id: The trade ID returned by log_entry().
            exit_price: The price at which the position was closed.
            pnl: Profit/loss in INR for this trade.
            reason: Exit reason — "target_hit", "sl_hit", "trailing_sl",
                    or "eod_squareoff".
        """
        now = format_ist(get_ist_now())

        with self._lock:
            # Update SQLite
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    UPDATE trades
                    SET exit_price = ?, pnl = ?, exit_reason = ?, status = 'CLOSED'
                    WHERE id = ?
                    """,
                    (exit_price, pnl, reason, trade_id),
                )
                conn.commit()

            # Append exit row to CSV
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade_id, now, "", "", "", "", "", "", exit_price,
                    "", pnl, reason, "", "",
                ])

        logger.info(
            "Trade #%d EXIT logged: exit=%.2f pnl=%.2f reason=%s",
            trade_id, exit_price, pnl, reason,
        )

    def get_daily_summary(self) -> dict:
        """
        Get a summary of today's closed trades.

        Returns:
            Dict with keys: total_trades, winners, losers, total_pnl.
        """
        today = get_ist_now().strftime("%Y-%m-%d")

        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT pnl FROM trades
                    WHERE timestamp LIKE ? AND status = 'CLOSED'
                    """,
                    (f"{today}%",),
                ).fetchall()

        pnls = [r[0] for r in rows if r[0] is not None]
        winners = sum(1 for p in pnls if p > 0)
        losers = sum(1 for p in pnls if p <= 0)
        total_pnl = sum(pnls)

        return {
            "total_trades": len(pnls),
            "winners": winners,
            "losers": losers,
            "total_pnl": round(total_pnl, 2),
        }

    def get_open_trades(self) -> list[dict]:
        """
        Get all currently open trades.

        Returns:
            List of dicts with trade details.
        """
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status = 'OPEN'"
                ).fetchall()
                return [dict(row) for row in rows]
