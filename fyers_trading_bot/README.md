# Fyers Algorithmic Trading Bot

A production-ready, fully autonomous algorithmic trading system for Indian markets (NSE) using the Fyers API.

## Features

- 📊 **Real-time data** via Fyers WebSocket v3
- 🕯️ **OHLCV candle building** with configurable timeframes (1m, 5m, 15m)
- 📈 **Multiple strategies**: Opening Range Breakout (ORB) + Smart Money Concepts (SMC)
- 🛡️ **Strict risk management**: position sizing, daily loss caps, RR filters
- ⚡ **Live order execution** via Fyers REST API with trailing stop loss
- 📱 **Telegram alerts** on every trade event
- 📝 **Trade logging** to both CSV and SQLite
- 🔄 **Auto-reconnect** on WebSocket disconnections
- 🧪 **DRY_RUN mode** for paper trading (no real orders)

## Prerequisites

- **Python 3.10+**
- **Fyers trading account** with API access
- **Fyers API app** created at [myapi.fyers.in](https://myapi.fyers.in)
- **Telegram bot** (optional, for alerts)

## Quick Start

### 1. Clone & Install

```bash
cd fyers_trading_bot
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```ini
FYERS_CLIENT_ID=YOUR_APP_ID-100
FYERS_SECRET_KEY=YOUR_SECRET_KEY
FYERS_REDIRECT_URI=https://trade.fyers.in/api-login/redirect-uri/index.html
FYERS_ACCESS_TOKEN=

TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Get Fyers API Credentials

1. Go to [myapi.fyers.in](https://myapi.fyers.in) and log in
2. Create a new app → note the **App ID** and **Secret Key**
3. Set the **Redirect URI** to `https://trade.fyers.in/api-login/redirect-uri/index.html`

### 4. Get Telegram Bot Token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** from BotFather's response
4. To get your **chat ID**: send a message to your bot, then visit:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   Look for the `"chat": {"id": 123456789}` field

### 5. Run the Bot

```bash
python main.py
```

On first run, the bot will:
1. Open a Fyers authentication URL in your browser
2. Ask you to paste the `auth_code` from the redirect URL
3. Save the token for the rest of the trading day

## Configuration

All settings are in **`config.py`**:

| Setting | Default | Description |
|---------|---------|-------------|
| `SYMBOLS` | NIFTY50, BANKNIFTY | Symbols to trade |
| `CANDLE_TIMEFRAME_MINUTES` | 5 | Candle duration |
| `RISK_PER_TRADE_PCT` | 1.0% | Max risk per trade as % of capital |
| `MIN_REWARD_RISK_RATIO` | 2.0 | Minimum RR ratio |
| `MAX_DAILY_LOSS_INR` | ₹5,000 | Hard daily loss cap |
| `MAX_TRADES_PER_DAY` | 5 | Max trades per day |
| `CAPITAL` | ₹1,00,000 | Total trading capital |
| `MARKET_OPEN` | 09:15 | Market open time (IST) |
| `MARKET_CLOSE` | 15:20 | Stop new entries time (IST) |
| `DRY_RUN` | `True` | Paper trade mode |

## Strategies

### Opening Range Breakout (ORB)

- Captures the first 30 minutes (9:15–9:45) as the "opening range"
- Signals on close breaking above/below the range with volume confirmation
- Includes Previous Day High/Low (PDH/PDL) confluence scoring
- One signal per symbol per day; no signals after 2:00 PM

### Smart Money Concepts (SMC)

- Detects institutional order flow patterns:
  - **Market Structure**: Break of Structure (BOS) & Change of Character (CHoCH)
  - **Order Blocks**: Last opposing candle before a structural break
  - **Fair Value Gaps**: 3-candle imbalance zones
- Confluence scoring: minimum 3 out of 4 (OB + FVG + bias + volume) required

## Adding a New Strategy

1. Create a new file in `strategies/`, e.g. `my_strategy.py`
2. Inherit from `BaseStrategy`:

```python
from strategies.base_strategy import BaseStrategy

class MyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "my_strategy"

    def analyze(self, symbol, df):
        # Your logic here
        # Return signal dict or None
        pass
```

3. Register it in `strategies/strategy_manager.py`:

```python
from strategies.my_strategy import MyStrategy

_STRATEGY_REGISTRY["my_strategy"] = MyStrategy
```

4. Add `"my_strategy"` to `ACTIVE_STRATEGIES` in `config.py`

## Project Structure

```
fyers_trading_bot/
├── main.py                   # Entry point
├── config.py                 # All settings
├── auth/
│   └── fyers_auth.py         # OAuth2 login flow
├── data/
│   ├── websocket_client.py   # WebSocket connection
│   └── candle_builder.py     # Tick → OHLCV candles
├── strategies/
│   ├── base_strategy.py      # Abstract base class
│   ├── range_breakout.py     # ORB strategy
│   ├── smc_strategy.py       # SMC strategy
│   └── strategy_manager.py   # Loads & runs strategies
├── risk/
│   └── risk_manager.py       # Position sizing & limits
├── execution/
│   └── order_manager.py      # Fyers order placement
├── notifications/
│   └── telegram_alert.py     # Telegram alerts
├── logger/
│   └── trade_logger.py       # CSV + SQLite logging
├── utils/
│   └── time_utils.py         # IST timezone helpers
├── requirements.txt
├── .env.example
└── README.md
```

## Risk Management

The bot enforces multiple layers of risk control:

1. **Per-trade risk**: Max 1% of capital (configurable)
2. **Reward-risk ratio**: Minimum 1:2 required
3. **Daily loss cap**: Hard stop at ₹5,000 daily loss
4. **Trade count limit**: Max 5 trades per day
5. **Market hours**: No new entries outside 9:15 AM – 3:20 PM IST
6. **Trailing stop loss**: Moves to breakeven after 1× risk profit
7. **EOD square-off**: All positions closed at 3:20 PM IST

## ⚠️ Important Warnings

> **🔴 PAPER TRADE FIRST!** Always test with `DRY_RUN=True` before going live.
> The default setting is `DRY_RUN=True` — set to `False` in `config.py` only
> when you are confident the bot works correctly.

> **🔴 FINANCIAL RISK** Algorithmic trading involves substantial risk of loss.
> Past performance does not guarantee future results. Only trade with money
> you can afford to lose.

> **🔴 NO WARRANTY** This software is provided "as is" without warranty of
> any kind. The authors are not responsible for any financial losses.

## Logs & Data

- **Console**: Real-time heartbeat showing market status, positions, PnL
- **Log file**: `trading_bot.log` — full debug-level logging
- **Trades CSV**: `trades_log.csv` — one row per trade entry/exit
- **Trades DB**: `logger/trades.db` — SQLite with full trade history

## License

MIT License — use at your own risk.
