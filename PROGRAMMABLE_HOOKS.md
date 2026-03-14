# Programmable Copy Trading Hooks

This document describes how to configure programmable copy trading hooks for the Polymarket copy trading system.

## Overview

Hooks allow you to customize how trades are copied from leader wallets to your account. You can set:

- **Amount calculation**: Fixed, percentage, proportional, or custom formula
- **Trade filters**: Min/max trade size, price ranges
- **Outcome filters**: Only copy specific outcomes (Yes/No)
- **Market filters**: Only copy from specific markets
- **Risk management**: Max daily loss, max trades per hour
- **Custom conditions**: Python-like expressions for complex logic

## Hook Configuration (JSON)

Store this in the `config` column of `copy_trading_hooks` table:

```json
{
  "leader_address": "0x...",

  // Amount Calculation
  "amount_mode": "proportional",
  "fixed_usd_amount": 5.0,
  "percentage": 50.0,
  "amount_formula": "leader_amount * 0.5 + 1",

  // Trade Size Filters
  "min_trade_size": 10.0,
  "max_trade_size": 1000.0,

  // Price Filters
  "min_price": 0.1,
  "max_price": 0.9,

  // Outcome Filters
  "allowed_outcomes": ["Yes"],
  "blocked_outcomes": ["No"],

  // Market Filters (substring match on market slug)
  "allowed_markets": ["bitcoin", "eth"],
  "blocked_markets": ["sports"],

  // Side Filters
  "allowed_sides": ["BUY", "SELL"],

  // Risk Management
  "max_daily_loss": 100.0,
  "max_position_size": 50.0,
  "max_trades_per_hour": 10,

  // Custom Condition (Python expression)
  "condition": "price < 0.7 and size > 5"
}
```

## Amount Modes

### 1. `fixed`
Always copy with a fixed USD amount.

```json
{"amount_mode": "fixed", "fixed_usd_amount": 5.0}
```

### 2. `percentage`
Copy as a percentage of the leader's trade size.

```json
{"amount_mode": "percentage", "percentage": 10.0}
```

### 3. `proportional` (default)
Copy proportionally based on leader's trade.

### 4. `formula`
Custom formula using `leader_amount`, `price`, `size` variables.

```json
{"amount_mode": "formula", "amount_formula": "leader_amount * 0.5"}
```

## Condition Expressions

Conditions use Python-like syntax with these variables available:

- `amount` - Trade amount in USD
- `price` - Price per share
- `size` - Number of shares
- `outcome` - Outcome string (e.g., "Yes", "No")
- `side` - Trade side ("BUY", "SELL")

Example conditions:

```python
# Only copy if price is below 0.7
price < 0.7

# Only copy large trades
size > 10

# Combined condition
price < 0.7 and size > 5

# Copy only Yes outcomes
outcome == "Yes"

# Copy trades in price range
0.2 < price < 0.8
```

## Risk Management

### Max Daily Loss
Stop copying after losing a certain amount per day:

```json
{"max_daily_loss": 100.0}
```

### Max Trades Per Hour
Limit the number of copy trades:

```json
{"max_trades_per_hour": 10}
```

## Examples

### Example 1: Conservative Copy
Copy 10% of leader trades, only for trades > $20, max $50/day loss.

```json
{
  "leader_address": "0x...",
  "amount_mode": "percentage",
  "percentage": 10.0,
  "min_trade_size": 20.0,
  "max_daily_loss": 50.0
}
```

### Example 2: Aggressive Copy
Copy all trades proportionally, no filters.

```json
{
  "leader_address": "0x...",
  "amount_mode": "proportional"
}
```

### Example 3: Selective Copy
Only copy Yes outcomes, price between 0.3-0.7, fixed $5 per trade.

```json
{
  "leader_address": "0x...",
  "amount_mode": "fixed",
  "fixed_usd_amount": 5.0,
  "allowed_outcomes": ["Yes"],
  "min_price": 0.3,
  "max_price": 0.7
}
```

### Example 4: Conditional Copy
Only copy if price < 0.5 AND size > 10.

```json
{
  "leader_address": "0x...",
  "amount_mode": "fixed",
  "fixed_usd_amount": 2.0,
  "condition": "price < 0.5 and size > 10"
}
```

## Database Setup

To create a hook via SQL:

```sql
INSERT INTO copy_trading_hooks (
    follower_user_id,
    leader_user_id,
    created_at,
    config,
    enabled
) VALUES (
    123,  -- Your user ID
    NULL, -- Null for global wallet hooks
    strftime('%s', 'now'),
    '{"leader_address":"0x...","amount_mode":"percentage","percentage":25.0}',
    1
);
```

## API Endpoints

### Reload Hooks
After updating hooks manually, reload the tracker:

```bash
curl -X POST http://localhost:7050/admin/copy-trading/reload-hooks
```

### Test a Hook

```bash
curl -X POST "http://localhost:7050/admin/copy-trading/test-hook?hook_id=1"
```

## Notes

- Hooks are loaded from the database at startup and when you call `/reload-hooks`
- `enabled = 1` is required for hooks to be active
- `leader_address` must be lowercase in the config
- Use `/copy` in the Telegram bot to manage hooks via UI
