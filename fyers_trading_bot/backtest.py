"""
Backtester for the Fyers Trading Bot — Options Edition.

Fetches historical index candle data, runs strategies to detect signals,
then fetches ATM option candle data to simulate actual option premium
PnL with premium-based SL and target.

Usage:
    python3.10 backtest.py                        # Today's data
    python3.10 backtest.py --date 2026-04-22      # Specific date
    python3.10 backtest.py --from 2026-04-15 --to 2026-04-22  # Date range
    python3.10 backtest.py --symbol NSE:NIFTY50-INDEX         # Single symbol
"""

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from fyers_apiv3 import fyersModel

# ─── Setup Python path ──────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    CANDLE_TIMEFRAME_MINUTES,
    CAPITAL,
    ENTRY_CUTOFF,
    FYERS_CLIENT_ID,
    LOT_SIZES,
    MAX_DAILY_LOSS_INR,
    MAX_POSITIONS_PER_SYMBOL,
    MAX_TRADES_PER_DAY,
    MIN_REWARD_RISK_RATIO,
    OPTION_SL_PCT,
    OPTION_TARGET_PCT,
    RISK_PER_TRADE_PCT,
    SIGNAL_COOLDOWN_MINUTES,
    SYMBOLS,
    TOKEN_FILE,
    TRAIL_ACTIVATION_PCT,
    TRAIL_DISTANCE_PCT,
)
from strategies.range_breakout import RangeBreakoutStrategy
from strategies.smc_strategy import SMCStrategy
from utils.time_utils import IST
from utils.option_utils import (
    get_atm_strike,
    get_option_symbol_for_signal,
    get_option_lot_size,
    get_option_symbol_with_fallback,
)

# ─── Logging ─────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# ANSI colors
# ═══════════════════════════════════════════════════════════════════════════════
class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"


# ═══════════════════════════════════════════════════════════════════════════════
# Simulated Option Position
# ═══════════════════════════════════════════════════════════════════════════════
class SimulatedPosition:
    """Tracks a simulated option position during backtesting."""

    def __init__(
        self,
        index_symbol: str,
        option_symbol: str,
        direction: str,
        atm_strike: int,
        option_type: str,
        entry_premium: float,
        qty: int,
        strategy: str,
        reason: str,
        entry_time: datetime,
        index_entry: float,
        index_sl: float,
        index_target: float,
        confidence: float = 0.0,
    ) -> None:
        self.index_symbol = index_symbol
        self.option_symbol = option_symbol
        self.direction = direction          # BUY/SELL (on index)
        self.atm_strike = atm_strike
        self.option_type = option_type      # CE or PE
        self.entry_premium = entry_premium
        self.qty = qty
        self.strategy = strategy
        self.reason = reason
        self.entry_time = entry_time
        self.confidence = confidence

        # Index-level reference (for display only)
        self.index_entry = index_entry
        self.index_sl = index_sl
        self.index_target = index_target

        # Premium-based SL and target
        self.sl_premium = round(entry_premium * (1 - OPTION_SL_PCT / 100), 2)
        self.target_premium = round(entry_premium * (1 + OPTION_TARGET_PCT / 100), 2)

        # Trailing SL state
        self.peak_premium = entry_premium   # highest premium seen
        self.lowest_premium = entry_premium # lowest premium seen (MAE)
        self.trailing_stage = 0             # 0=none, 1=breakeven, 2=lock profit
        self.original_sl = self.sl_premium
        self.candles_in_trade = 0           # count of candles while position open

        # Exit tracking
        self.exit_premium: Optional[float] = None
        self.exit_time: Optional[datetime] = None
        self.exit_reason: Optional[str] = None
        self.pnl: float = 0.0

    def check_option_candle(self, candle: dict) -> bool:
        """
        Check if SL, trailing SL, or target is hit by this option candle.

        Progressive Trailing SL:
        - Activates when premium gains >= TRAIL_ACTIVATION_PCT (default 10%)
        - Once active, SL = peak_premium × (1 - TRAIL_DISTANCE_PCT/100)
        - As premium rises, SL ratchets up continuously
        - SL never moves down (only up)

        Example with 10% trail distance:
          Entry ₹200, Peak ₹260 (+30%) → SL = 260 × 0.90 = ₹234 (+17% locked)
          Entry ₹200, Peak ₹240 (+20%) → SL = 240 × 0.90 = ₹216 (+8% locked)

        Returns True if position was closed.
        """
        high = candle["high"]
        low = candle["low"]
        ts = candle["timestamp"]

        self.candles_in_trade += 1

        # Update peak and trough
        if high > self.peak_premium:
            self.peak_premium = high
        if low < self.lowest_premium:
            self.lowest_premium = low

        # Calculate peak gain percentage from entry
        gain_pct = (self.peak_premium - self.entry_premium) / self.entry_premium * 100

        # Progressive trailing SL (using config values)
        if gain_pct >= TRAIL_ACTIVATION_PCT:
            # Calculate new trailing SL level
            new_trail_sl = round(self.peak_premium * (1 - TRAIL_DISTANCE_PCT / 100), 2)

            # SL only ratchets up, never down
            if new_trail_sl > self.sl_premium:
                self.sl_premium = new_trail_sl

            # Track trailing stage for display
            if self.sl_premium > self.entry_premium * 1.01:
                self.trailing_stage = 2   # profit locked
            elif self.sl_premium >= self.entry_premium * 0.99:
                self.trailing_stage = 1   # breakeven

        # SL hit (option premium dropped)
        if low <= self.sl_premium:
            exit_reason = "sl_hit"
            if self.trailing_stage >= 2:
                locked_pct = (self.sl_premium - self.entry_premium) / self.entry_premium * 100
                exit_reason = f"trailing_sl_+{locked_pct:.0f}%"
            elif self.trailing_stage == 1:
                exit_reason = "trailing_sl_breakeven"
            self._close(self.sl_premium, ts, exit_reason)
            return True

        # Target hit (option premium rose)
        if high >= self.target_premium:
            self._close(self.target_premium, ts, "target_hit")
            return True

        return False

    def force_close(self, premium: float, ts: datetime) -> None:
        """Force close at EOD using option close price."""
        self._close(premium, ts, "eod_squareoff")

    def _close(self, exit_premium: float, ts: datetime, reason: str) -> None:
        """Close the position and calculate PnL."""
        self.exit_premium = exit_premium
        self.exit_time = ts
        self.exit_reason = reason
        # Always long the option: PnL = (exit - entry) × qty
        self.pnl = (exit_premium - self.entry_premium) * self.qty


# ═══════════════════════════════════════════════════════════════════════════════
# Data Fetching
# ═══════════════════════════════════════════════════════════════════════════════
def load_access_token() -> str:
    """Load access token from token.json."""
    token_path = os.path.join(os.path.dirname(__file__), TOKEN_FILE)
    if not os.path.exists(token_path):
        print(f"{C.RED}❌ No token.json found. Run main.py first to authenticate.{C.RESET}")
        sys.exit(1)
    with open(token_path) as f:
        data = json.load(f)
    token = data.get("access_token", "")
    if not token:
        print(f"{C.RED}❌ Empty token. Run main.py to re-auth.{C.RESET}")
        sys.exit(1)
    return token


def fetch_candles(
    fyers: fyersModel.FyersModel,
    symbol: str,
    date_str: str,
    resolution: str = "5",
) -> pd.DataFrame:
    """Fetch OHLCV candles for a symbol on a given date."""
    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": date_str,
        "range_to": date_str,
        "cont_flag": "1",
    }
    response = fyers.history(data=data)
    if response.get("s") != "ok" or not response.get("candles"):
        return pd.DataFrame()

    df = pd.DataFrame(
        response["candles"],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)

    # Deduplicate: Fyers sometimes returns duplicate candles
    df = df.drop_duplicates(subset=["timestamp"], keep="first").reset_index(drop=True)

    return df


# Option candle cache to avoid re-fetching the same symbol
_option_cache: dict[str, pd.DataFrame] = {}


def fetch_option_candles(
    fyers: fyersModel.FyersModel,
    option_symbol: str,
    date_str: str,
    resolution: str = "5",
) -> pd.DataFrame:
    """Fetch option OHLCV candles, with caching."""
    cache_key = f"{option_symbol}_{date_str}_{resolution}"
    if cache_key in _option_cache:
        return _option_cache[cache_key]

    df = fetch_candles(fyers, option_symbol, date_str, resolution)
    _option_cache[cache_key] = df
    return df


def find_option_candle_at_time(
    option_df: pd.DataFrame,
    target_time: datetime,
) -> Optional[dict]:
    """Find the option candle closest to the given timestamp."""
    if option_df.empty:
        return None

    # Find the candle with timestamp <= target_time
    mask = option_df["timestamp"] <= target_time
    if not mask.any():
        return option_df.iloc[0].to_dict()

    return option_df[mask].iloc[-1].to_dict()


def get_option_candles_after(
    option_df: pd.DataFrame,
    after_time: datetime,
) -> pd.DataFrame:
    """Get all option candles after a given time (for SL/target monitoring)."""
    if option_df.empty:
        return pd.DataFrame()
    return option_df[option_df["timestamp"] > after_time].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Backtest Engine
# ═══════════════════════════════════════════════════════════════════════════════
def run_backtest_for_date(
    fyers: fyersModel.FyersModel,
    symbols: list[str],
    date_str: str,
    resolution: str = "5",
) -> list[SimulatedPosition]:
    """Run backtest for a single date across all symbols."""

    # Parse entry cutoff time
    cutoff_h, cutoff_m = map(int, ENTRY_CUTOFF.split(":"))

    print(f"\n{C.BOLD}{C.CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  📅 BACKTEST: {date_str}  (SL: -{OPTION_SL_PCT}% | Target: +{OPTION_TARGET_PCT}% | Cutoff: {ENTRY_CUTOFF})")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C.RESET}\n")

    # Clear option cache for new date
    _option_cache.clear()

    strategies = [RangeBreakoutStrategy(), SMCStrategy()]
    all_positions: list[SimulatedPosition] = []
    daily_pnl = 0.0
    trade_count = 0

    # Parse date for option symbol construction
    trade_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)

    # Track last signal time per symbol for cooldown
    last_signal_time: dict[str, datetime] = {}

    for index_symbol in symbols:
        print(f"  {C.BOLD}📊 {index_symbol}{C.RESET}")

        # Fetch index candle data
        index_df = fetch_candles(fyers, index_symbol, date_str, resolution)
        if index_df.empty or len(index_df) < 10:
            print(f"    {C.DIM}Insufficient data ({len(index_df)} candles). Skipping.{C.RESET}")
            continue

        print(f"    {C.DIM}Loaded {len(index_df)} candles "
              f"({index_df.iloc[0]['timestamp'].strftime('%H:%M')} → "
              f"{index_df.iloc[-1]['timestamp'].strftime('%H:%M')}){C.RESET}")

        # Replay index candles through strategies
        open_positions: list[SimulatedPosition] = []
        skipped_reasons: dict[str, int] = {}  # track skip reasons

        for i in range(6, len(index_df)):
            candle = index_df.iloc[i].to_dict()
            history_df = index_df.iloc[:i + 1].reset_index(drop=True)
            candle_time = candle["timestamp"]

            # Check open positions against option candles at this time
            for pos in open_positions[:]:
                opt_df = fetch_option_candles(fyers, pos.option_symbol, date_str, resolution)
                opt_candle = find_option_candle_at_time(opt_df, candle_time)
                if opt_candle and pos.check_option_candle(opt_candle):
                    open_positions.remove(pos)
                    daily_pnl += pos.pnl
                    _print_exit(pos)

            # Skip if daily limits reached
            if trade_count >= MAX_TRADES_PER_DAY:
                continue
            if daily_pnl < 0 and abs(daily_pnl) >= MAX_DAILY_LOSS_INR:
                continue

            # ── FILTER 1: Entry cutoff ──
            if candle_time.hour > cutoff_h or (candle_time.hour == cutoff_h and candle_time.minute >= cutoff_m):
                continue

            # Run strategies on index data
            for strategy in strategies:
                signal = strategy.analyze(index_symbol, history_df)
                if signal is None:
                    continue

                # ── FILTER 2: Max positions per symbol ──
                symbol_positions = [p for p in open_positions if p.index_symbol == index_symbol]
                if len(symbol_positions) >= MAX_POSITIONS_PER_SYMBOL:
                    skipped_reasons["max_positions"] = skipped_reasons.get("max_positions", 0) + 1
                    continue

                # ── FILTER 3: Signal cooldown ──
                last_time = last_signal_time.get(index_symbol)
                if last_time and (candle_time - last_time).total_seconds() < SIGNAL_COOLDOWN_MINUTES * 60:
                    skipped_reasons["cooldown"] = skipped_reasons.get("cooldown", 0) + 1
                    continue

                # Get ATM option symbol (with fallback for expired months)
                atm_strike = get_atm_strike(signal["entry_price"], index_symbol)
                option_type = "CE" if signal["direction"] == "BUY" else "PE"
                option_symbol = get_option_symbol_with_fallback(
                    index_symbol, atm_strike, option_type, trade_date,
                    fyers=fyers, date_str=date_str,
                )

                # Fetch option candles for the ATM strike
                opt_df = fetch_option_candles(fyers, option_symbol, date_str, resolution)
                if opt_df.empty:
                    print(f"    {C.DIM}⚠ No option data for {option_symbol}. Skipping.{C.RESET}")
                    continue

                # Find the option premium at signal time
                entry_candle = find_option_candle_at_time(opt_df, candle_time)
                if entry_candle is None:
                    continue

                entry_premium = entry_candle["close"]
                if entry_premium <= 0:
                    continue

                # Calculate lot size
                lot_size = get_option_lot_size(index_symbol)
                risk_per_trade = CAPITAL * (RISK_PER_TRADE_PCT / 100)
                premium_risk = entry_premium * (OPTION_SL_PCT / 100)
                lots = max(1, math.floor(risk_per_trade / (premium_risk * lot_size)))
                qty = lots * lot_size

                # Create position
                pos = SimulatedPosition(
                    index_symbol=index_symbol,
                    option_symbol=option_symbol,
                    direction=signal["direction"],
                    atm_strike=atm_strike,
                    option_type=option_type,
                    entry_premium=entry_premium,
                    qty=qty,
                    strategy=signal["strategy"],
                    reason=signal.get("reason", ""),
                    entry_time=candle_time,
                    index_entry=signal["entry_price"],
                    index_sl=signal["sl"],
                    index_target=signal["target"],
                    confidence=signal.get("confidence_score", 0),
                )
                open_positions.append(pos)
                all_positions.append(pos)
                trade_count += 1
                last_signal_time[index_symbol] = candle_time

                _print_entry(pos, lots)
                break  # one signal per candle

        # Print skipped signal stats
        if skipped_reasons:
            skip_str = ", ".join(f"{k}: {v}" for k, v in skipped_reasons.items())
            print(f"    {C.DIM}🚫 Skipped signals: {skip_str}{C.RESET}")

        # EOD square-off
        if open_positions and not index_df.empty:
            last_time = index_df.iloc[-1]["timestamp"]
            for pos in open_positions:
                opt_df = fetch_option_candles(fyers, pos.option_symbol, date_str, resolution)
                if not opt_df.empty:
                    last_opt = find_option_candle_at_time(opt_df, last_time)
                    exit_prem = last_opt["close"] if last_opt else pos.entry_premium
                else:
                    exit_prem = pos.entry_premium
                pos.force_close(exit_prem, last_time)
                daily_pnl += pos.pnl
                _print_exit(pos)

    _print_daily_summary(date_str, all_positions, daily_pnl)
    return all_positions


# ═══════════════════════════════════════════════════════════════════════════════
# Display Helpers
# ═══════════════════════════════════════════════════════════════════════════════
def _print_entry(pos: SimulatedPosition, lots: int) -> None:
    """Print a trade entry."""
    emoji = "🟢" if pos.direction == "BUY" else "🔴"
    color = C.GREEN if pos.direction == "BUY" else C.RED
    opt_label = f"ATM {pos.option_type} ({pos.atm_strike})"

    print(f"\n    {emoji} {color}{C.BOLD}{pos.direction} SIGNAL{C.RESET} → "
          f"{C.BOLD}{opt_label}{C.RESET} — {pos.strategy.upper()}")
    print(f"    {C.DIM}┌─ Time:       {pos.entry_time.strftime('%H:%M')}{C.RESET}")
    print(f"    {C.DIM}├─ Option:     {pos.option_symbol}{C.RESET}")
    print(f"    {C.DIM}├─ Premium:    ₹{pos.entry_premium:,.2f}{C.RESET}")
    print(f"    {C.DIM}├─ SL:         ₹{pos.sl_premium:,.2f} (-{OPTION_SL_PCT:.0f}%)  |  "
          f"Target: ₹{pos.target_premium:,.2f} (+{OPTION_TARGET_PCT:.0f}%){C.RESET}")
    print(f"    {C.DIM}├─ Qty:        {pos.qty} ({lots} lot{'s' if lots > 1 else ''}){C.RESET}")
    print(f"    {C.DIM}├─ Index:      {pos.index_entry:,.2f} → SL {pos.index_sl:,.2f} / T {pos.index_target:,.2f}{C.RESET}")
    print(f"    {C.DIM}├─ Conf:       {pos.confidence:.0f}/4{C.RESET}")
    print(f"    {C.DIM}└─ Reason:     {pos.reason}{C.RESET}")


def _print_exit(pos: SimulatedPosition) -> None:
    """Print a trade exit with optimization metrics."""
    pnl_color = C.GREEN if pos.pnl >= 0 else C.RED
    emoji = "✅" if pos.pnl >= 0 else "❌"
    reason_labels = {
        "target_hit": "🎯 Target Hit",
        "sl_hit": "🛑 SL Hit",
        "trailing_sl_breakeven": "🔄 Trail → Breakeven",
        "eod_squareoff": "🕐 EOD Square-off",
    }
    # Handle dynamic trailing SL reasons like "trailing_sl_+14%"
    if pos.exit_reason and pos.exit_reason.startswith("trailing_sl_+"):
        reason_text = f"🔒 Trail → {pos.exit_reason.replace('trailing_sl_', '')} Locked"
    else:
        reason_text = reason_labels.get(pos.exit_reason, pos.exit_reason)

    prem_change = ((pos.exit_premium - pos.entry_premium) / pos.entry_premium * 100
                   if pos.entry_premium > 0 else 0)
    exit_time = pos.exit_time.strftime('%H:%M') if pos.exit_time else "N/A"
    peak_pct = ((pos.peak_premium - pos.entry_premium) / pos.entry_premium * 100
                if pos.entry_premium > 0 else 0)
    mae_pct = ((pos.entry_premium - pos.lowest_premium) / pos.entry_premium * 100
               if pos.entry_premium > 0 else 0)

    # Hold duration
    hold_min = 0
    if pos.exit_time and pos.entry_time:
        delta = (pos.exit_time - pos.entry_time).total_seconds() / 60
        hold_min = max(0, int(delta))  # ensure non-negative

    print(f"    {emoji} {pnl_color}EXIT ₹{pos.exit_premium:,.2f} ({prem_change:+.1f}%){C.RESET}"
          f" | PnL: {pnl_color}₹{pos.pnl:+,.2f}{C.RESET}"
          f" | {reason_text}"
          f" | ⏱{hold_min}m"
          f" | Peak +{peak_pct:.1f}%"
          f" | MAE -{mae_pct:.1f}%"
          f" | {exit_time}")


def _print_daily_summary(
    date_str: str,
    positions: list[SimulatedPosition],
    daily_pnl: float,
) -> None:
    """Print daily summary with optimization metrics."""
    closed = [p for p in positions if p.exit_premium is not None]
    winners = [p for p in closed if p.pnl > 0]
    losers = [p for p in closed if p.pnl <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0

    pnl_color = C.GREEN if daily_pnl >= 0 else C.RED
    emoji = "🏆" if daily_pnl >= 0 else "📉"

    print(f"\n{C.BOLD}{C.CYAN}  ─── SUMMARY for {date_str} ───{C.RESET}")
    print(f"  {emoji} Net PnL:     {pnl_color}{C.BOLD}₹{daily_pnl:+,.2f}{C.RESET}")
    print(f"  📊 Trades:     {len(closed)}")
    print(f"  ✅ Winners:    {len(winners)}")
    print(f"  ❌ Losers:     {len(losers)}")
    print(f"  📈 Win Rate:   {win_rate:.1f}%")

    if closed:
        avg_win = sum(p.pnl for p in winners) / len(winners) if winners else 0
        avg_loss = sum(p.pnl for p in losers) / len(losers) if losers else 0
        total_wins = sum(p.pnl for p in winners)
        total_losses = abs(sum(p.pnl for p in losers))
        profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
        pf_color = C.GREEN if profit_factor >= 1.5 else C.YELLOW if profit_factor >= 1 else C.RED

        # Peak & MAE stats
        peak_pcts = [(p.peak_premium - p.entry_premium) / p.entry_premium * 100
                     for p in closed if p.entry_premium > 0]
        mae_pcts = [(p.entry_premium - p.lowest_premium) / p.entry_premium * 100
                    for p in closed if p.entry_premium > 0]
        avg_peak = sum(peak_pcts) / len(peak_pcts) if peak_pcts else 0
        max_peak = max(peak_pcts) if peak_pcts else 0
        avg_mae = sum(mae_pcts) / len(mae_pcts) if mae_pcts else 0

        # Hold time
        hold_mins = [max(0, int((p.exit_time - p.entry_time).total_seconds() / 60))
                     for p in closed if p.exit_time and p.entry_time]
        avg_hold = sum(hold_mins) / len(hold_mins) if hold_mins else 0

        print(f"  💰 Avg Win:    {C.GREEN}₹{avg_win:+,.2f}{C.RESET}")
        print(f"  💸 Avg Loss:   {C.RED}₹{avg_loss:+,.2f}{C.RESET}")
        print(f"  📐 Profit Factor: {pf_color}{profit_factor:.2f}{C.RESET}")
        print(f"  ⏱️  Avg Hold:  {avg_hold:.0f} min")
        print(f"  📈 Avg Peak:   +{avg_peak:.1f}%  |  Max Peak: +{max_peak:.1f}%")
        print(f"  📉 Avg MAE:    -{avg_mae:.1f}%")

        # Exit type breakdown
        exit_types: dict[str, int] = {}
        for p in closed:
            r = p.exit_reason or "unknown"
            if r.startswith("trailing_sl_+"):
                r = "trailing_profit"
            exit_types[r] = exit_types.get(r, 0) + 1
        exit_str = " | ".join(f"{k}: {v}" for k, v in sorted(exit_types.items()))
        print(f"  🏷️  Exits:     {exit_str}")
    print()


def _print_multi_day_summary(all_positions: list[SimulatedPosition]) -> None:
    """Print multi-day summary with full optimization metrics."""
    if not all_positions:
        return

    closed = [p for p in all_positions if p.exit_premium is not None]
    total_pnl = sum(p.pnl for p in closed)
    winners = [p for p in closed if p.pnl > 0]
    losers = [p for p in closed if p.pnl <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    pnl_color = C.GREEN if total_pnl >= 0 else C.RED

    print(f"\n{C.BOLD}{C.MAGENTA}{'═' * 70}")
    print(f"  📊 MULTI-DAY BACKTEST SUMMARY (Options)")
    print(f"{'═' * 70}{C.RESET}")

    # --- Core Metrics ---
    print(f"  💰 Total PnL:      {pnl_color}{C.BOLD}₹{total_pnl:+,.2f}{C.RESET}")
    print(f"  📊 Total Trades:   {len(closed)}")
    print(f"  ✅ Winners:        {len(winners)}")
    print(f"  ❌ Losers:         {len(losers)}")
    print(f"  📈 Win Rate:       {win_rate:.1f}%")

    if winners:
        best = max(winners, key=lambda p: p.pnl)
        print(f"  🏆 Best Trade:     {C.GREEN}₹{best.pnl:+,.2f}{C.RESET} "
              f"({best.option_symbol} via {best.strategy})")
    if losers:
        worst = min(losers, key=lambda p: p.pnl)
        print(f"  💀 Worst Trade:    {C.RED}₹{worst.pnl:+,.2f}{C.RESET} "
              f"({worst.option_symbol} via {worst.strategy})")

    if closed:
        # --- Risk Metrics ---
        total_wins = sum(p.pnl for p in winners)
        total_losses = abs(sum(p.pnl for p in losers))
        profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
        avg_win = total_wins / len(winners) if winners else 0
        avg_loss = total_losses / len(losers) if losers else 0
        pf_color = C.GREEN if profit_factor >= 1.5 else C.YELLOW if profit_factor >= 1 else C.RED

        max_drawdown = 0.0
        running_pnl = 0.0
        peak_equity = 0.0
        for p in closed:
            running_pnl += p.pnl
            peak_equity = max(peak_equity, running_pnl)
            max_drawdown = max(max_drawdown, peak_equity - running_pnl)

        print(f"\n  {C.BOLD}── Risk ──{C.RESET}")
        print(f"  📐 Profit Factor:  {pf_color}{profit_factor:.2f}{C.RESET}")
        print(f"  💰 Avg Win:        {C.GREEN}₹{avg_win:+,.2f}{C.RESET}")
        print(f"  💸 Avg Loss:       {C.RED}₹{avg_loss:,.2f}{C.RESET}")
        print(f"  📉 Max Drawdown:   {C.RED}₹{max_drawdown:,.2f}{C.RESET}")
        print(f"  💹 ROI:            {pnl_color}{total_pnl / CAPITAL * 100:+.2f}%{C.RESET}")

        # --- Premium Analysis ---
        peak_pcts = [(p.peak_premium - p.entry_premium) / p.entry_premium * 100
                     for p in closed if p.entry_premium > 0]
        mae_pcts = [(p.entry_premium - p.lowest_premium) / p.entry_premium * 100
                    for p in closed if p.entry_premium > 0]
        avg_peak = sum(peak_pcts) / len(peak_pcts) if peak_pcts else 0
        max_peak = max(peak_pcts) if peak_pcts else 0
        avg_mae = sum(mae_pcts) / len(mae_pcts) if mae_pcts else 0
        max_mae = max(mae_pcts) if mae_pcts else 0

        print(f"\n  {C.BOLD}── Premium Analysis ──{C.RESET}")
        print(f"  📈 Avg Peak Gain:  +{avg_peak:.1f}%")
        print(f"  🔝 Max Peak Gain:  +{max_peak:.1f}%")
        print(f"  📉 Avg MAE:        -{avg_mae:.1f}%  (max adverse excursion)")
        print(f"  ⬇️  Max MAE:        -{max_mae:.1f}%")

        # --- Timing Analysis ---
        hold_mins = [max(0, int((p.exit_time - p.entry_time).total_seconds() / 60))
                     for p in closed if p.exit_time and p.entry_time]
        avg_hold = sum(hold_mins) / len(hold_mins) if hold_mins else 0
        max_hold = max(hold_mins) if hold_mins else 0
        min_hold = min(hold_mins) if hold_mins else 0

        morning = [p for p in closed if p.entry_time and p.entry_time.hour < 12]
        afternoon = [p for p in closed if p.entry_time and 12 <= p.entry_time.hour < 14]
        late = [p for p in closed if p.entry_time and p.entry_time.hour >= 14]
        m_pnl = sum(p.pnl for p in morning)
        a_pnl = sum(p.pnl for p in afternoon)
        l_pnl = sum(p.pnl for p in late)
        m_color = C.GREEN if m_pnl >= 0 else C.RED
        a_color = C.GREEN if a_pnl >= 0 else C.RED
        l_color = C.GREEN if l_pnl >= 0 else C.RED

        print(f"\n  {C.BOLD}── Timing ──{C.RESET}")
        print(f"  ⏱️  Avg Hold:       {avg_hold:.0f} min  (min: {min_hold}, max: {max_hold})")
        print(f"  🌅 Morning (<12):  {len(morning)} trades  {m_color}₹{m_pnl:+,.2f}{C.RESET}")
        print(f"  ☀️  Afternoon:      {len(afternoon)} trades  {a_color}₹{a_pnl:+,.2f}{C.RESET}")
        print(f"  🌆 Late (>14):     {len(late)} trades  {l_color}₹{l_pnl:+,.2f}{C.RESET}")

        # --- Exit Type Breakdown ---
        exit_types: dict[str, list] = {}
        for p in closed:
            r = p.exit_reason or "unknown"
            if r.startswith("trailing_sl_+"):
                r = "trailing_profit"
            if r not in exit_types:
                exit_types[r] = []
            exit_types[r].append(p.pnl)

        print(f"\n  {C.BOLD}── Exit Types ──{C.RESET}")
        for etype, pnls in sorted(exit_types.items(), key=lambda x: len(x[1]), reverse=True):
            e_pnl = sum(pnls)
            e_color = C.GREEN if e_pnl >= 0 else C.RED
            print(f"  {'':4s}{etype:25s} {len(pnls):2d} trades  {e_color}₹{e_pnl:+,.2f}{C.RESET}")

        # --- Streaks ---
        max_win_streak = 0
        max_loss_streak = 0
        current_streak = 0
        for p in closed:
            if p.pnl > 0:
                current_streak = max(0, current_streak) + 1
                max_win_streak = max(max_win_streak, current_streak)
            else:
                current_streak = min(0, current_streak) - 1
                max_loss_streak = max(max_loss_streak, abs(current_streak))

        print(f"\n  {C.BOLD}── Streaks ──{C.RESET}")
        print(f"  🔥 Max Win Streak:  {max_win_streak}")
        print(f"  ❄️  Max Loss Streak: {max_loss_streak}")

    print(f"{C.MAGENTA}{'═' * 70}{C.RESET}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest strategies with ATM options on historical data.",
    )
    parser.add_argument("--date", "-d", type=str, default=None)
    parser.add_argument("--from", "-f", dest="date_from", type=str, default=None)
    parser.add_argument("--to", "-t", dest="date_to", type=str, default=None)
    parser.add_argument("--symbol", "-s", type=str, default=None)
    parser.add_argument("--resolution", "-r", type=str,
                        default=str(CANDLE_TIMEFRAME_MINUTES))
    args = parser.parse_args()

    access_token = load_access_token()
    fyers = fyersModel.FyersModel(
        client_id=FYERS_CLIENT_ID, is_async=False,
        token=access_token, log_path="",
    )

    symbols = [args.symbol] if args.symbol else SYMBOLS
    today = datetime.now(IST).strftime("%Y-%m-%d")

    if args.date_from and args.date_to:
        start = datetime.strptime(args.date_from, "%Y-%m-%d")
        end = datetime.strptime(args.date_to, "%Y-%m-%d")
        dates = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        print(f"\n{C.BOLD}{C.MAGENTA}🔬 MULTI-DAY OPTIONS BACKTEST: "
              f"{args.date_from} → {args.date_to} ({len(dates)} days){C.RESET}")
    elif args.date:
        dates = [args.date]
    else:
        dates = [today]

    all_positions: list[SimulatedPosition] = []
    for date_str in dates:
        positions = run_backtest_for_date(fyers, symbols, date_str, args.resolution)
        all_positions.extend(positions)

    if len(dates) > 1:
        _print_multi_day_summary(all_positions)


if __name__ == "__main__":
    main()
