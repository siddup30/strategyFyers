"""
FastAPI backend for the Fyers Trading Bot UI dashboard.

Endpoints:
  GET  /                    → serve index.html
  GET  /api/status          → bot status, WS, positions, PnL
  GET  /api/token           → token validity info
  GET  /api/auth/url        → Fyers auth URL
  POST /api/auth            → submit auth_code, generate token
  POST /api/bot/start       → start bot process
  POST /api/bot/stop        → stop bot process
  GET  /api/logs/stream     → SSE live log stream
  POST /api/backtest        → run backtest, return JSON results
  GET  /api/trades          → trade history from SQLite
  GET  /api/config          → current config values
  POST /api/config          → save config values
"""

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ANSI escape code stripper (backtest output uses color codes)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

def strip_ansi(text: str) -> str:
    """Remove ANSI terminal color codes from a string."""
    return _ANSI_RE.sub("", text)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent          # fyers_trading_bot/
STATIC_DIR = Path(__file__).parent / "static"
TOKEN_FILE = BASE_DIR / "token.json"
LOG_FILE = BASE_DIR / "trading_bot.log"
DB_FILE = BASE_DIR / "logger" / "trades.db"
CONFIG_FILE = BASE_DIR / "config.py"
CONFIG_JSON = BASE_DIR / "ui" / "config_overrides.json"  # persisted UI edits

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="StrategyFyers Dashboard", version="1.0.0")

# ── Bot process handle ─────────────────────────────────────────────────────────
_bot_process: Optional[subprocess.Popen] = None


# ══════════════════════════════════════════════════════════════════════════════
# Static files
# ══════════════════════════════════════════════════════════════════════════════

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ══════════════════════════════════════════════════════════════════════════════
# /api/status
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/status")
def get_status():
    """Return bot running state, WS status, positions, PnL."""
    global _bot_process

    running = _bot_process is not None and _bot_process.poll() is None

    # Parse heartbeat from last log line to get WS status, positions, PnL
    ws_connected = False
    positions = 0
    trades = 0
    pnl = 0.0
    last_heartbeat = None

    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(errors="ignore").splitlines()
            for line in reversed(lines[-200:]):
                if "💓" in line or "Market:" in line:
                    last_heartbeat = line.strip()
                    ws_connected = "WS: ✅" in line
                    # Parse: Positions: 0 | Trades: 0 | PnL: ₹+0.00
                    m = re.search(r"Positions:\s*(\d+)", line)
                    if m:
                        positions = int(m.group(1))
                    m = re.search(r"Trades:\s*(\d+)", line)
                    if m:
                        trades = int(m.group(1))
                    m = re.search(r"PnL:\s*₹([+\-\d.,]+)", line)
                    if m:
                        try:
                            pnl = float(m.group(1).replace(",", ""))
                        except ValueError:
                            pass
                    break
        except Exception:
            pass

    # Dry run mode from log
    dry_run = True
    if LOG_FILE.exists():
        try:
            content = LOG_FILE.read_text(errors="ignore")
            # find last DRY_RUN line
            for line in reversed(content.splitlines()[-500:]):
                if "DRY_RUN:" in line:
                    dry_run = "True" in line
                    break
        except Exception:
            pass

    return {
        "running": running,
        "dry_run": dry_run,
        "ws_connected": ws_connected,
        "open_positions": positions,
        "trades_today": trades,
        "pnl_today": pnl,
        "last_heartbeat": last_heartbeat,
        "pid": _bot_process.pid if running else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# /api/token
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/token")
def get_token_status():
    """Return Fyers token validity info."""
    if not TOKEN_FILE.exists():
        return {"valid": False, "reason": "No token file found"}

    try:
        data = json.loads(TOKEN_FILE.read_text())
        token_date = data.get("date", "")
        today = datetime.now().strftime("%Y-%m-%d")
        valid = token_date == today
        return {
            "valid": valid,
            "date": token_date,
            "today": today,
            "user": data.get("user", ""),
            "reason": None if valid else f"Token is from {token_date}, needs refresh",
        }
    except Exception as e:
        return {"valid": False, "reason": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# /api/auth
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/auth/url")
def get_auth_url():
    """Return Fyers auth URL for the user to open in browser."""
    sys.path.insert(0, str(BASE_DIR.parent))
    os.chdir(str(BASE_DIR))
    try:
        from auth.fyers_auth import get_auth_url as _get_url
        url = _get_url()
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AuthCodeRequest(BaseModel):
    auth_code: str


@app.post("/api/auth")
def submit_auth_code(body: AuthCodeRequest):
    """Exchange auth_code for access token and save to token.json."""
    sys.path.insert(0, str(BASE_DIR.parent))
    os.chdir(str(BASE_DIR))
    try:
        from auth.fyers_auth import exchange_auth_code
        result = exchange_auth_code(body.auth_code)
        return {"success": True, "user": result.get("user", "")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# /api/bot
# ══════════════════════════════════════════════════════════════════════════════

class BotStartRequest(BaseModel):
    dry_run: bool = True


@app.post("/api/bot/start")
def start_bot(body: BotStartRequest):
    """Start the trading bot as a subprocess."""
    global _bot_process

    if _bot_process is not None and _bot_process.poll() is None:
        return {"success": False, "message": "Bot is already running"}

    # Apply dry_run override to config_overrides.json
    overrides = _load_overrides()
    overrides["DRY_RUN"] = body.dry_run
    _save_overrides(overrides)
    _apply_overrides_to_config(overrides)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE_DIR.parent)

    _bot_process = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "main.py")],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    mode = "DRY RUN" if body.dry_run else "LIVE"
    return {"success": True, "message": f"Bot started in {mode} mode", "pid": _bot_process.pid}


@app.post("/api/bot/stop")
def stop_bot():
    """Stop the trading bot gracefully."""
    global _bot_process

    if _bot_process is None or _bot_process.poll() is not None:
        return {"success": False, "message": "Bot is not running"}

    try:
        _bot_process.send_signal(signal.SIGINT)
        _bot_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _bot_process.kill()
    except Exception as e:
        return {"success": False, "message": str(e)}

    _bot_process = None
    return {"success": True, "message": "Bot stopped"}


# ══════════════════════════════════════════════════════════════════════════════
# /api/logs/stream  (SSE)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/logs/stream")
async def stream_logs(request: Request, lines: int = 100):
    """SSE endpoint: tail the log file and stream new lines to browser."""

    async def event_generator():
        # Send last N lines first
        if LOG_FILE.exists():
            all_lines = LOG_FILE.read_text(errors="ignore").splitlines()
            for line in all_lines[-lines:]:
                yield f"data: {json.dumps(line)}\n\n"

        # Then tail for new lines
        last_size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0

        while True:
            if await request.is_disconnected():
                break

            await asyncio.sleep(1)

            if not LOG_FILE.exists():
                continue

            current_size = LOG_FILE.stat().st_size
            if current_size > last_size:
                with open(LOG_FILE, errors="ignore") as f:
                    f.seek(last_size)
                    new_content = f.read()
                last_size = current_size
                for line in new_content.splitlines():
                    if line.strip():
                        yield f"data: {json.dumps(line)}\n\n"
            elif current_size < last_size:
                # File was rotated / cleared
                last_size = current_size

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# /api/backtest
# ══════════════════════════════════════════════════════════════════════════════

class BacktestRequest(BaseModel):
    symbol: str = "NSE:NIFTY50-INDEX"
    date: str = ""           # single day  YYYY-MM-DD
    date_from: str = ""      # range start YYYY-MM-DD
    date_to: str = ""        # range end   YYYY-MM-DD
    resolution: int = 5      # candle timeframe in minutes


@app.post("/api/backtest")
def run_backtest(body: BacktestRequest):
    """Run the backtest script and return structured results."""
    is_range = bool(body.date_from and body.date_to)
    date_str = body.date or datetime.now().strftime("%Y-%m-%d")

    cmd = [
        sys.executable,
        str(BASE_DIR / "backtest.py"),
        "--symbol", body.symbol,
        "--resolution", str(body.resolution),
    ]

    if is_range:
        cmd += ["--from", body.date_from, "--to", body.date_to]
    else:
        cmd += ["--date", date_str]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": str(BASE_DIR.parent)},
        )
        raw = result.stdout + result.stderr
        # Strip ANSI color codes before parsing — backtest uses colored output
        output = strip_ansi(raw)

        # Use findall + last match so multi-day summary totals override per-day values
        def last(pattern, text, group=1):
            matches = re.findall(pattern, text)
            return matches[-1] if matches else None

        trades_raw  = last(r"(?:Total\s+)?Trades:\s*(\d+)",   output)
        pnl_raw     = last(r"(?:Total|Net)\s+PnL:\s*[₹]?([+\-][\d,]+\.\d+|[\d,]+\.\d+)", output)
        winners_raw = last(r"Winners:\s*(\d+)",               output)
        losers_raw  = last(r"Losers:\s*(\d+)",                output)
        winrate_raw = last(r"Win Rate:\s*([\d.]+)%",           output)
        candles_raw = last(r"Loaded (\d+) candles",            output)

        net_pnl = 0.0
        if pnl_raw:
            try:
                net_pnl = float(pnl_raw.replace(",", ""))
            except ValueError:
                pass

        # Count trading days from range
        days_run = 1
        if is_range:
            from datetime import timedelta
            try:
                d0 = datetime.strptime(body.date_from, "%Y-%m-%d")
                d1 = datetime.strptime(body.date_to, "%Y-%m-%d")
                days_run = sum(
                    1 for i in range((d1 - d0).days + 1)
                    if (d0 + timedelta(days=i)).weekday() < 5
                )
            except Exception:
                pass

        return {
            "success": result.returncode == 0,
            "symbol": body.symbol,
            "date": date_str if not is_range else f"{body.date_from} → {body.date_to}",
            "resolution": body.resolution,
            "days_run": days_run,
            "candles_loaded": int(candles_raw) if candles_raw else 0,
            "trades":   int(trades_raw)   if trades_raw  else 0,
            "net_pnl":  net_pnl,
            "winners":  int(winners_raw)  if winners_raw else 0,
            "losers":   int(losers_raw)   if losers_raw  else 0,
            "win_rate": float(winrate_raw) if winrate_raw else 0.0,
            "raw_output": output,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Backtest timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# /api/trades
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/trades")
def get_trades(limit: int = 100, status: str = "ALL"):
    """Return trade history from SQLite."""
    if not DB_FILE.exists():
        return {"trades": [], "total": 0}

    try:
        with sqlite3.connect(str(DB_FILE)) as conn:
            conn.row_factory = sqlite3.Row
            if status == "ALL":
                rows = conn.execute(
                    "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()

        trades = [dict(r) for r in rows]
        return {"trades": trades, "total": len(trades)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# /api/config
# ══════════════════════════════════════════════════════════════════════════════

# Fields exposed in the UI (subset of config.py)
CONFIG_FIELDS = {
    "DRY_RUN": bool,
    "CAPITAL": float,
    "RISK_PER_TRADE_PCT": float,
    "MAX_DAILY_LOSS_INR": float,
    "MAX_TRADES_PER_DAY": int,
    "OPTION_SL_PCT": float,
    "OPTION_TARGET_PCT": float,
    "TRAIL_ACTIVATION_PCT": float,
    "TRAIL_DISTANCE_PCT": float,
    "SIGNAL_COOLDOWN_MINUTES": int,
    "MAX_POSITIONS_PER_SYMBOL": int,
    "ENTRY_CUTOFF": str,
    "CANDLE_TIMEFRAME_MINUTES": int,
}


def _load_overrides() -> dict:
    if CONFIG_JSON.exists():
        try:
            return json.loads(CONFIG_JSON.read_text())
        except Exception:
            pass
    return {}


def _save_overrides(data: dict):
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_JSON.write_text(json.dumps(data, indent=2))


def _read_config_values() -> dict:
    """Read current values from config.py by importing it."""
    import importlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("config", str(CONFIG_FILE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    result = {}
    for key in CONFIG_FIELDS:
        result[key] = getattr(mod, key, None)
    return result


def _apply_overrides_to_config(overrides: dict):
    """Patch config.py with override values using regex replacement."""
    if not overrides:
        return

    content = CONFIG_FILE.read_text()

    for key, value in overrides.items():
        if key not in CONFIG_FIELDS:
            continue
        field_type = CONFIG_FIELDS[key]

        if field_type == bool:
            new_val = "True" if value else "False"
            pattern = rf"^({key}\s*:\s*bool\s*=\s*).*$"
            replacement = rf"\g<1>{new_val}"
        elif field_type == float:
            new_val = f"{float(value)}"
            pattern = rf"^({key}\s*:\s*float\s*=\s*).*$"
            replacement = rf"\g<1>{new_val}"
        elif field_type == int:
            new_val = str(int(value))
            pattern = rf"^({key}\s*:\s*int\s*=\s*).*$"
            replacement = rf"\g<1>{new_val}"
        elif field_type == str:
            new_val = f'"{value}"'
            pattern = rf"^({key}\s*:\s*str\s*=\s*).*$"
            replacement = rf"\g<1>{new_val}"
        else:
            continue

        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)

    CONFIG_FILE.write_text(content)


@app.get("/api/config")
def get_config():
    """Return current editable config values."""
    try:
        values = _read_config_values()
        overrides = _load_overrides()
        # Merge: overrides win over config.py values
        values.update(overrides)
        return {"config": values, "fields": list(CONFIG_FIELDS.keys())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config")
def save_config(body: dict):
    """Save config overrides. Applied to config.py on next bot start."""
    try:
        # Validate and coerce types
        cleaned = {}
        for key, raw_value in body.items():
            if key not in CONFIG_FIELDS:
                continue
            field_type = CONFIG_FIELDS[key]
            if field_type == bool:
                cleaned[key] = bool(raw_value)
            elif field_type == float:
                cleaned[key] = float(raw_value)
            elif field_type == int:
                cleaned[key] = int(raw_value)
            elif field_type == str:
                cleaned[key] = str(raw_value)

        _save_overrides(cleaned)
        return {"success": True, "saved": cleaned}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
