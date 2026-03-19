# AutoTrader - Instant Signal Trading

The autotrader executes trades automatically based on Telegram signals using the logic from `scripts/autotrader.py`.

## Quick Start

### Single-User Mode

```bash
# Set environment variables
export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
export TELEGRAM_SIGNAL_CHAT=@your_signal_channel
export USER_ID=123
export DB_PATH=app_data.sqlite3

# Run for a single user (1 second poll for instant execution)
python scripts/autotrader.py --user-id 123 --amount 3

# Dry run mode
python scripts/autotrader.py --user-id 123 --amount 3 --dry-run
```

### Multi-User Mode (Production)

Run for **all enabled users** at once. Each user trades with their own configured amount.

```bash
# Set environment variables
export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
export TELEGRAM_SIGNAL_CHAT=@your_signal_channel
export DB_PATH=app_data.sqlite3

# Run for all enabled users
python scripts/autotrader.py --all-users

# Dry run mode
python scripts/autotrader.py --all-users --dry-run
```

### AutoTrader Listener (Direct Telegram)

```bash
export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
export TELEGRAM_SIGNAL_CHAT=@signal_channel
export USER_ID=123

python scripts/autotrader_listener.py --user-id 123 --amount 3
```

## How It Works

1. **Signal Reception**: Listens to Telegram channel for signal messages
2. **Signal Parsing**: Extracts asset, timeframe, and direction (YES/NO)
3. **Market Resolution**: Resolves the correct Polymarket market for the timeframe
4. **Trade Execution**: Executes limit order immediately
5. **Tracking**: Records traded markets per user to prevent re-trading

## Supported Timeframes

- `1m` - 1 minute
- `5m` - 5 minutes
- `15m` - 15 minutes
- `30m` - 30 minutes
- `1h` - 1 hour

## Settings

### User Settings (via Bot)

Users enable signal trading and set their trade amount via the bot:
1. `/menu` → `Signal trading`
2. Tap `Enable`
3. Set number of shares (5, 10, 20, or custom)

### Limit Orders

- **Default limit price**: $0.55 per share
- **Minimum shares**: 5
- **Share calculation**: `max(amount_usd / limit_price, min_shares)`

## API Endpoints

### Test Signal Trading

```bash
curl -X POST http://localhost:8000/me/signal-trading/test \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"timeframe": "5m", "side": "YES", "amount_usd": 3}'
```

### Autotrader Endpoints

- `POST /me/autotrader/execute` - Execute a signal trade
- `POST /me/autotrader/reset` - Reset tracked markets for current user
- `GET /me/autotrader/status` - Get autotrader status
- `GET /autotrader/timeframes` - List supported timeframes
- `GET /autotrader/markets` - List available markets

## Bot Commands

### Manual Autotrader Command

```
/autotrader SIGNAL SIDE TIMEFRAME [AMOUNT]

Examples:
/autotrader BTC YES 5m
/autotrader BTC NO 15m 10
```

## Multi-User Support

- Each user has independent trade amounts
- Each user tracks their own traded markets
- Sequential execution (one user at a time)
- Database-backed tracking persists across restarts

## Troubleshooting

### "User not found"
Set USER_ID correctly or ensure user exists in database.

### "No enabled users found"
Users need to enable signal trading in the bot.

### "Trade failed"
Check USDC.e balance in Safe wallet. Use `/balance` in bot.
