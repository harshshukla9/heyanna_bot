## Anna / Polymarket HTTP API – Frontend Reference

This document summarizes the HTTP endpoints exposed by the FastAPI app in `api_app.py`, grouped by feature area. All routes are JSON unless otherwise noted.

- **Base URL (local dev)**: `http://localhost:8000`
- **Auth**: most `/me/*`, `/copy-trading/*`, and trading endpoints require a **JWT Bearer token** obtained via the Telegram login flow.
  - Header: `Authorization: Bearer <jwt>`

---

### Auth & Sessions

- **GET `/health`**
  - Health check. No auth.

- **GET `/test/login`** (HTML)
  - Dev-only Telegram Login Widget test page. No auth.

- **POST `/auth/telegram`**
  - Body: `init_data: string` (Telegram Mini App `initData`).
  - Response: `{ "token": "<jwt>" }`.

- **POST `/auth/manual`** (dev only)
  - Body: `{ "user_id": number }`.
  - Response: `{ "token": "<jwt>", "session_id": "..." }`.

- **POST `/auth/logout`** (auth)
  - Revokes the current session.
  - Response: `{ "success": true }`.

- **GET `/auth/telegram-widget`**
  - Callback used by `/test/login`; returns `{ "token": "<jwt>" }`.

---

### User & Wallet

All require auth (`Authorization: Bearer <jwt>`).

- **GET `/me`**
  - Returns basic user info and wallet:
  - Example response:
    - `{ "user_id": 123, "username": "alice", "eth_address": "0x...", "copy_trading_enabled": true }`.

- **GET `/me/status`**
  - Flags:
    - `is_copytrading: boolean`
    - `polymarket_approved: boolean`.

- **GET `/me/wallet/address`**
  - `{ "eth_address": "0x..." }`.

- **GET `/me/wallet/private-key`** (unsafe, dev only)
  - Returns `{ "eth_private_key": "0x..." }`.

- **GET `/me/balance`**
  - Polygon token balances + total USD:
    - `{ "wallet": "0x...", "tokens": [{ "symbol": "USDC", "balance": 123.45, "usd_value": 123.45 }], "total_usd": 123.45 }`.

---

### Portfolio & Public Profiles

- **GET `/me/portfolio`** (auth)
  - Full structured portfolio for current user:
    - `balance`, `positions`, `closed_positions`, `markets`, `summary`, and `orders`.

- **GET `/users/{address}/portfolio`** (public)
  - Same structure as `/me/portfolio` but for any wallet.
  - Only uses Polymarket Data API (no local DB trades).

- **GET `/users/{address}/profile`** (public)
  - Proxies `https://gamma-api.polymarket.com/public-profile?address={address}`.
  - Returns profile fields like `name`, `bio`, `profileImage`, `verifiedBadge`, etc.

- **POST `/me/claim-winnings`** (auth)
  - Triggers gasless redemption of resolved Polymarket markets via relayer.
  - Response: `{ "wallet": "0x...", "result": "human-readable status" }`.

- **POST `/me/approve`** (auth)
  - Manually enforces the Polymarket gasless approval flow for the current user.
  - Runs Safe deployment (if needed) + USDC/CTF approvals via Builder relayer.
  - Response:
    - `{ "wallet": "0x...", "approved": true|false, "result": "human-readable status" }`.

---

### Bridge (Deposits into Polymarket)

- **POST `/bridge/deposit`** (auth)
  - Returns Polymarket Bridge deposit addresses for the user:
    - `{ "polymarket_wallet": "0x...", "bridge_addresses": {...}, "bridge_options": [...] }`.

- **GET `/bridge/supported-assets`** (public)
  - Static list of supported chains & tokens for the bridge:
    - `{ "supportedAssets": [{ "chainName": "Polygon", "tokens": ["USDC", "USDT", ...] }, ...] }`.

---

### Markets & Prices

- **GET `/markets/trending`** (public)
  - Returns `{ "markets": [ { "condition_id", "question", "outcomes", "odds_cents", "end_date", ... } ] }`.

- **GET `/markets/search?q=string`** (public)
  - Search by keyword; returns same `markets` shape.

- **GET `/markets/category/{category}`** (public)
  - Filter by Gamma tag slug (e.g. `politics`, `crypto`).

- **GET `/markets/tag/{tag_id}?include_related=false`** (public)
  - Filter by numeric tag ID (sports / other tags).

- **GET `/price/{condition_id}`** (public)
  - Current decimal prices for all outcomes of a market.

- **GET `/prices/history/{condition_id}`** (public, defined later in file)
  - Historical price series for a market from the CLOB API.

- **GET `/market/{condition_id}`** (public, defined later in file)
  - Full cached market object for a condition.

---

### Polymarket Data API Proxies & Feeds

- **GET `/data/trades`** (public)
  - Query Polymarket Data API `/trades`.
  - Query params:
    - `user`, `market`, `eventId`, `side`, `filterType`, `filterAmount`, `takerOnly`, `limit`, `offset`.
  - Response:
    - `{ "trades": [...] }` with normalized `tx_hash` / `tx_id`.

- **GET `/trades`** (public)
  - Combined global feed (local DB trades + Polymarket Data API for registered users).
  - Query: `limit`, `offset`.
  - Response:
    - `{ "trades": [...], "limit": n, "offset": m }`.

- **POST `/trades`** (auth)
  - Record a matched trade in the local `trades` table for the feed.
  - Body (`PostTradeRequest`):
    ```json
    {
      "condition_id": "string",
      "side": "Yes|No",
      "amount": 10.0,
      "order_id": "string",
      "tx_hash": "0x...",
      "tx_id": "0x...",
      "size": 5.0,
      "price": 0.6,
      "order_side": "BUY|SELL"
    }
    ```
  - Requires non-`pending` `tx_hash`/`tx_id`.

---

### Copy Trading – Follower Controls

All require auth.

- **POST `/me/copy-trading/enable`**
  - Enable copy trading for current user (`copy_trading_enabled=1`).

- **POST `/me/copy-trading/disable`**
  - Disable copy trading (`copy_trading_enabled=0`).

- **GET `/me/copy-trading`**
  - Returns:
    - `{ "copy_trading_enabled": bool, "following": [ { "user_id", "username", "eth_address", "copy_trading_enabled" } ], "following_count": n }`.

- **POST `/copy-trading/follow`**
  - Follow a leader (local user or global Polymarket address). Body:
    ```json
    {
      "leader_username": "string",
      "leader_address": "string",
      "size_multiplier": 1,
      "max_usd_per_trade": 0,
      "fractional": true,
      "mode": "fractional | one_to_one | beginner",
      "fixed_usd_amount": 1,
      "max_loss_pct": 0,
      "slippage_pct": 0
    }
    ```
  - Response includes hook metadata and whether it’s a global profile (`global: true/false`).
  - Tracker hooks are reloaded immediately after follow, so new follows begin without restart.

- **POST `/copy-trading/unfollow`**
  - Body: same shape as `/copy-trading/follow`, but you typically set either:
    - `leader_username` **or** `leader_address` to target which leader to unfollow.
  - For address-based global follows, removal is now precise per hook (prevents deleting unrelated hooks).
  - Tracker hooks are reloaded immediately after unfollow.

- **GET `/copy-trading/following`**
  - List leaders the current user is following (local users).

- **GET `/copy-trading/hooks`**
  - List all hooks (local + global) for current user:
    - Includes `config` with mode, risk settings, leader address, etc.

---

### Copy Trading – Admin / Background

These are meant for cron / backend automation; require auth and should be protected.

- **POST `/admin/copy-trading/global-tick`**
  - Query/body: `limit_per_leader` (default 50).
  - Runs **one** global copy-trading tick:
    - For each enabled hook with `config.leader_address`, pulls recent trades from Polymarket Data API.
    - Applies mode (`fractional`, `one_to_one`, `beginner`), `size_multiplier`, `max_usd_per_trade`, `slippage_pct`.
    - Places mirrored trades for followers via `execute_trade_for_user`.
  - Response:
    - `{ "processed_hooks": n, "mirrored_trades": m }`.

- **POST `/admin/copy-trading/stop-loss-tick`**
  - Body/query: `default_max_loss_pct` (default 15).
  - For each follower with hooks that have `max_loss_pct > 0`:
    - Fetches their Polymarket positions.
    - If a position’s PnL ≤ `-max_loss_pct`, auto-SELLs the full position via `execute_trade_for_user`.
  - Response:
    - `{ "followers_with_stop_loss": n, "positions_closed": m }`.

---

### Bridge / System Admin

- **POST `/admin/trades/flush-invalid`** (auth)
  - Deletes local trades without valid `tx_hash` (null/empty/`pending`).
  - Response: `{ "deleted": n, "message": "..." }`.

---

### Notes for Frontend Integration

- **Auth flow**:
  - Mini App → `POST /auth/telegram` with `initData` → store `token` in memory or secure storage.
  - Include `Authorization: Bearer <token>` for all protected endpoints.

- **Error handling**:
  - Standard FastAPI errors: `{ "detail": "..." }` with appropriate HTTP status.
  - For Polymarket/Data/Bridge proxies, you may see `502` with `detail` describing upstream failure.

- **Copy-trading config**:
  - `mode` is the main UX switch:
    - `"fractional"` → same % of USDC.e balance as leader.
    - `"one_to_one"` → same USD amount as leader.
    - `"beginner"` → fixed small USD amount (`fixed_usd_amount`, default 1).
  - `max_loss_pct` and `slippage_pct` are optional safety rails used by the backend indexer/stop-loss ticks.

