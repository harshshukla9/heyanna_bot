## Anna / Polymarket REST API – Frontend Doc

This document summarizes the main HTTP endpoints exposed by the FastAPI app (`api_app.py`) for the frontend team.

- **Base URL (dev)**: `http://localhost:8000`
- **Auth header (when required)**:  
  `Authorization: Bearer <jwt>`

---

### 0. High-Level User Flow

This is how a typical user interacts with the system end-to-end:

1. **Authentication (Telegram Mini App)**
   - User opens the Telegram Mini App.
   - Telegram injects `initData` into the Mini App.
   - Frontend sends `initData` to `POST /auth/telegram`.
   - Backend verifies the signature, creates/loads a user + session, and returns a **JWT**.
   - Frontend stores this JWT (e.g. in memory) and adds it as `Authorization: Bearer <jwt>` on subsequent calls.

2. **Wallet & Balance**
   - Frontend calls:
     - `GET /me` to get `user_id`, `username`, `eth_address`.
     - `GET /me/balance` to show Polygon wallet balances & total USD.
   - This is used to render wallet info, portfolio header, and basic account state.

3. **Browse Markets & Prices**
   - Frontend calls one of:
     - `GET /markets/trending` for a default list.
     - `GET /markets/search?q=...` when the user searches.
     - `GET /markets/category/{category}` or `/markets/tag/{tag_id}` for filtered views.
   - For a single market detail page:
     - `GET /price/{condition_id}` for live odds.
     - Optionally `GET /prices/history/{condition_id}` for charts.

4. **Portfolio View**
   - For the logged-in user:
     - `GET /me/portfolio` → positions, closed positions, PnL summary, and orders.
   - For viewing another address (public profile / leaderboard row click):
     - `GET /users/{address}/portfolio`.
     - Optionally `GET /users/{address}/profile` to show Polymarket profile info (name/avatar/verified).

5. **Copy Trading Setup**
   - User enables copy trading:
     - `POST /me/copy-trading/enable`.
   - User follows a leader:
     - Frontend lets them choose:
       - A **local user** (by username), or
       - A **Polymarket wallet address** (global profile).
     - Frontend collects risk preferences:
       - `mode` (`fractional`, `one_to_one`, `beginner`),
       - `size_multiplier`, `fixed_usd_amount`,
       - `max_usd_per_trade`, `max_loss_pct`, `slippage_pct`.
     - Sends these in `POST /copy-trading/follow`.
   - User can manage:
     - `GET /copy-trading/hooks` to see all hooks + configs.
     - `POST /copy-trading/unfollow` to stop copying a leader.

6. **Trades Mirrored in Background**
   - A separate backend process / cron calls:
     - `POST /admin/copy-trading/global-tick` to mirror trades from followed Polymarket profiles.
     - `POST /admin/copy-trading/stop-loss-tick` to enforce stop-loss rules.
   - Frontend does **not** call these endpoints directly in normal user flows, but the effects appear in:
     - `GET /me/portfolio` (new positions/closed positions),
     - `GET /trades` (global feed).

7. **Claiming Winnings & Bridging**
   - When positions resolve:
     - User taps “Claim winnings” → frontend calls `POST /me/claim-winnings`.
  - If approvals need to be enforced manually:
    - Frontend can call `POST /me/approve`.
   - When depositing funds:
     - Frontend calls `POST /bridge/deposit` to get deposit addresses & supported assets UI.

This flow should give you a mental model for which endpoints are called at each step in the UX.

---

### 1. Auth & Session

- **GET `/health`**
  - Health check.
  - Response: `{ "status": "ok" }`

- **POST `/auth/telegram`**
  - Login via Telegram Mini App `initData`.
  - Body (JSON or form): `{ "init_data": "<telegram initData>" }`
  - Response:
    ```json
    { "token": "<jwt>" }
    ```

- **POST `/auth/manual`** (dev only)
  - Body:
    ```json
    { "user_id": 123 }
    ```
  - Response:
    ```json
    { "token": "<jwt>", "session_id": "..." }
    ```

- **POST `/auth/logout`** (auth)
  - Revokes current session.
  - Response: `{ "success": true }`

---

### 2. User & Wallet

All endpoints below require `Authorization: Bearer <jwt>`.

- **GET `/me`**
  - Current user profile + wallet.
  - Response:
    ```json
    {
      "user_id": 123,
      "username": "alice",
      "eth_address": "0x...",
      "copy_trading_enabled": true
    }
    ```

- **GET `/me/status`**
  - Response:
    ```json
    {
      "is_copytrading": true,
      "polymarket_approved": false
    }
    ```

- **GET `/me/wallet/address`**
  - Response:
    ```json
    { "eth_address": "0x..." }
    ```

- **GET `/me/balance`**
  - Current Polygon balances + totals.
  - Response shape:
    ```json
    {
      "wallet": "0x...",
      "tokens": [
        { "symbol": "USDC", "balance": 123.45, "usd_value": 123.45 }
      ],
      "total_usd": 123.45
    }
    ```

---

### 3. Portfolio & Public Profiles

- **GET `/me/portfolio`** (auth)
  - Full structured portfolio (positions, PnL, orders) for current user.
  - Important fields:
    - `positions[]` with `title`, `outcome`, `condition_id`, `size`, `avg_price`, `current_price`, `current_value`, `pnl_percent`, `pnl_cash`, `orders[]`.
    - `summary` with counts and total PnL.

- **GET `/users/{address}/portfolio`** (public)
  - Same shape as `/me/portfolio` for any wallet address (no local DB orders).

- **GET `/users/{address}/profile`** (public)
  - Polymarket public profile for wallet.

- **POST `/me/claim-winnings`** (auth)
  - Body: none.
  - Response:
    ```json
    {
      "wallet": "0x...",
      "result": "human readable status"
    }
    ```

- **POST `/me/approve`** (auth)
  - Body: none.
  - Runs Safe deployment (if required) and gasless USDC/CTF approvals.
  - Response:
    ```json
    {
      "wallet": "0x...",
      "approved": true,
      "result": "human readable status"
    }
    ```

---

### 4. Bridge

- **POST `/bridge/deposit`** (auth)
  - Returns Polymarket bridge deposit addresses for current user.

- **GET `/bridge/supported-assets`** (public)
  - Static list of supported chains and tokens.

---

### 5. Markets & Prices

- **GET `/markets/trending`** (public)
  - Response:
    ```json
    { "markets": [ { "condition_id", "question", "event_title", "outcomes", "token_ids", "odds_cents", "end_date" } ] }
    ```

- **GET `/markets/search?q=string`** (public)
  - Search active markets by keyword.

- **GET `/markets/category/{category}`** (public)
  - Markets filtered by category tag slug.

- **GET `/markets/tag/{tag_id}?include_related=false`** (public)
  - Markets by numeric tag id.

- **GET `/price/{condition_id}`** (public)
  - Current decimal prices per outcome for a market.

---

### 6. Trades & Data API Proxies

- **GET `/data/trades`** (public)
  - Proxy to Polymarket Data API `/trades`.
  - Query params:
    - `user`, `market`, `eventId`, `side`, `filterType`, `filterAmount`, `takerOnly`, `limit`, `offset`.
  - Response:
    ```json
    { "trades": [ /* trades with tx_hash + tx_id */ ] }
    ```

- **GET `/trades`** (public)
  - Combined global feed (local trades + Data API for registered users).
  - Query: `limit`, `offset`.

- **POST `/trades`** (auth)
  - Record a matched trade in local `trades` table (feed).
  - Body:
    ```json
    {
      "condition_id": "0x...",
      "side": "Yes",
      "amount": 10.0,
      "order_id": "string",
      "tx_hash": "0x...",
      "tx_id": "0x...",
      "size": 20.0,
      "price": 0.5,
      "order_side": "BUY"
    }
    ```

---

### 7. Copy Trading – Follower Controls

All require auth.

- **POST `/me/copy-trading/enable`**
  - Enables copy trading for user.

- **POST `/me/copy-trading/disable`**
  - Disables copy trading.

- **GET `/me/copy-trading`**
  - Returns:
    ```json
    {
      "copy_trading_enabled": true,
      "following": [ { "user_id", "username", "eth_address", "copy_trading_enabled" } ],
      "following_count": 1
    }
    ```

- **POST `/copy-trading/follow`**
  - Configure a follow hook for a leader (local user or global wallet).
  - Body:
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
  - Hooks are reloaded immediately so follows become active without restart.

- **POST `/copy-trading/unfollow`**
  - Same body shape; set `leader_username` or `leader_address` to target which leader to unfollow.
  - Address-based unfollow now removes the exact hook row (safe for global hooks).
  - Hooks are reloaded immediately after removal.

- **GET `/copy-trading/following`**
  - List followed leaders.

- **GET `/copy-trading/hooks`**
  - Full list of hooks for user, including `config` (mode, risk, leader_address, etc.).

---

### 8. Copy Trading – Admin / Background

Protect these endpoints; they are for cron / backend automation.

- **POST `/admin/copy-trading/global-tick`**
  - Query/body: `limit_per_leader` (default 50).
  - Runs one global copy-trading indexer tick.

- **POST `/admin/copy-trading/stop-loss-tick`**
  - Query/body: `default_max_loss_pct` (default 15).
  - Runs stop-loss across followers who configured `max_loss_pct` in hooks.

---

### 9. Admin Utility

- **POST `/admin/trades/flush-invalid`** (auth)
  - Deletes local trades with invalid `tx_hash` (null/empty/`pending`).

