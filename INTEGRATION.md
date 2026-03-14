## Integration checklist

High‑level checklist for wiring the Polymarket AI trading + copy‑trading stack in this repo.

### 1. Environment / secrets

- [ ] Set core env vars in `.env`:
  - [ ] `BOT_TOKEN` – Telegram bot token
  - [ ] `OPENAI_API_KEY` – LLM key (or dummy when using local endpoint)
  - [ ] `POLYGON_RPC_URL` – Polygon RPC (e.g. Alchemy, Infura, public RPC)
- [ ] (Optional) Covalent / GoldRush:
  - [ ] `COVALENT_API_KEY` – for richer Polygon balances
- [ ] Polymarket Builder (for future gasless relay – **do not hard‑code these**):
  - [ ] `POLY_BUILDER_API_KEY`
  - [ ] `POLY_BUILDER_SECRET`
  - [ ] `POLY_BUILDER_PASSPHRASE`

### 2. LLM / analysis

- [ ] Configure the LLM endpoint in `llm.py`:
  - [ ] `base_url` points to your OpenAI‑compatible server (e.g. local Ollama gateway)
  - [ ] `model="gpt-oss:120b"` (or your chosen model name)
- [ ] The Telegram bots (`bot_app.py`, `bot.py`) use:
  - [ ] `llm.get_chat_response(...)` for non‑streaming replies (snappy UX)
  - [ ] `active_market_context` so the LLM can resolve “this market / that side”.
- [ ] Market analysis endpoint:
  - [ ] `POST /analyze-market` (in `api_app.py`) calls `llm.run_market_analysis(...)`.
  - [ ] `run_market_analysis` in `llm.py`:
    - [ ] Performs **multi‑query news fetch** via `bot_tools.search_news` (DuckDuckGo news).
    - [ ] Runs a **multi‑agent pipeline**:
      - [ ] Researcher → clusters news into themes.
      - [ ] Bullish forecaster → strongest YES case.
      - [ ] Bearish forecaster → strongest NO case.
      - [ ] Final synthesizer → single, grounded forecast.
    - [ ] Returns Markdown with fixed sections:
      - [ ] `## Summary`
      - [ ] `## Data points`
      - [ ] `## Forecast & risk`
        - [ ] `Verdict: YES or NO`
        - [ ] `Implied odds: NN%`
        - [ ] `Risk score: NN/100`

### 3. News grounding

- [ ] `bot_tools.search_news(query, max_results)`:
  - [ ] Uses `ddgs.DDGS` to query DuckDuckGo News.
  - [ ] Prioritizes **newer news**:
    - [ ] Tries timelimits `d` (day), `w` (week), `m` (month), `y` (year).
    - [ ] Deduplicates `(title, date)` pairs.
    - [ ] Returns up to `max_results` items, newest first.
- [ ] `run_market_analysis`:
  - [ ] Calls `search_news` with multiple focused sub‑queries (`query`, `query latest`, `…protests`, `…diplomacy`, `…war`, `…sanctions`).
  - [ ] Aggregates up to ~20 articles into a compact bullet list fed into the LLM.

### 4. Polymarket data integration

Existing endpoints in `api_app.py`:

- [ ] Trades (Data API proxy):
  - [ ] `GET /polymarket/data/trades` – wraps `https://data-api.polymarket.com/trades` with filters:
    - [ ] `user`, `market`, `eventId`, `side`, `filterType`, `filterAmount`, `takerOnly`, `limit`, `offset`.
    - [ ] Normalizes `tx_hash` / `tx_id`.
- [ ] Global trade feed:
  - [ ] `GET /trades` – combines:
    - [ ] Local app‑recorded trades (`trades` table, with `tx_hash`).
    - [ ] Polymarket Data API trades for registered users (where appropriate).

Position/portfolio helpers (used by `/me/portfolio`):

- [ ] In `bot_tools.py`:
  - [ ] `_fetch_positions(address)` → `https://data-api.polymarket.com/positions?user={address}`.
  - [ ] `_fetch_closed_positions(address)` → closed positions endpoint.
  - [ ] `_build_portfolio_from_fetched(...)` computes:
    - [ ] Per‑position PnL.
    - [ ] Per‑market PnL.
    - [ ] Aggregate PnL and portfolio value.
  - [ ] `get_polymarket_portfolio_with_positions(address)`:
    - [ ] Returns a human‑readable summary + structured `positions` list (used by `/portfolio` and close‑position UI).

### 5. Trading & copy‑trading

Core trading (CLOB, `trading.py`):

- [ ] `execute_trade_for_user`:
  - [ ] Resolves `condition_id` → market via `market_cache`.
  - [ ] Uses `py_clob_client` with user’s `eth_private_key` to place **BUY/SELL** FOK market orders.
  - [ ] Records matched trades in `trades` table with size/price/order_side.
- [ ] `cancel_order_for_user`, `get_open_orders_for_user`:
  - [ ] Use CLOB API to cancel / list open orders.

Copy‑trading (in `api_app.py` + `database_manager.py`):

- [ ] Enable/disable for a user:
  - [ ] `POST /me/copy-trading/enable`
  - [ ] `POST /me/copy-trading/disable`
- [ ] Follow / unfollow **local** leaders by username:
  - [ ] `POST /copy-trading/follow` – creates a hook + row in `copy_trading_follows`.
  - [ ] `POST /copy-trading/unfollow`
  - [ ] `GET /copy-trading/following` / `/me/copy-trading` / `/copy-trading/hooks` – inspect state.
- [ ] Hook execution on trades:
  - [ ] `_maybe_schedule_copy_trade(...)`:
    - [ ] On successful leader trade, schedules `_fire_copy_hooks(...)`.
  - [ ] `_fire_copy_hooks(...)`:
    - [ ] For each follower hook:
      - [ ] Calls `execute_trade_for_user` with same `condition_id`, `side`, `amount`, `order_side`.
      - [ ] Marks `copied_from_user_id` so copied trades are distinguishable.
- [ ] Cancellation propagation:
  - [ ] `POST /trade/cancel`:
    - [ ] Cancels the leader’s CLOB order via `cancel_order_for_user`.
    - [ ] Schedules `_propagate_cancel_to_followers(...)`.
  - [ ] `_propagate_cancel_to_followers(...)`:
    - [ ] Resolves leader’s cancelled order (`token_id`, side) from CLOB.
    - [ ] For each follower:
      - [ ] Fetches their open orders.
      - [ ] Attempts to cancel any open order with matching `token_id` and side.

### 6. Leaderboards

- [ ] Local app leaderboard:
  - [ ] `GET /social/leaderboard`:
    - [ ] Aggregates `trades` table per user:
      - [ ] `trade_count`, `total_volume`, `open_volume`, `close_volume`, `first_trade_at`, `last_trade_at`.
    - [ ] Sorted by `total_volume` desc, paginated by `limit`.
- [ ] Local trade feed:
  - [ ] `GET /social/feed` – raw local trade list (for activity/feeds).

### 7. Telegram bot UX

Bot in `bot_app.py` (main entry used by `main.py`):

- [ ] Commands:
  - [ ] `/start`, `/wallet`, `/balance`, `/portfolio`, `/markets`, `/trending`, `/category`, `/swap`, `/approve`, `/close`, `/menu`, `/help`.
- [ ] Markets UI:
  - [ ] `/markets` and `/category` show inline buttons for markets.
  - [ ] Tapping a market updates `active_market_context` and shows Trade Yes/No buttons.
  - [ ] Tapping Trade Yes/No shows amount buttons: `$1, $5, $10, $20, $50, $100`.
- [ ] Amount shortcuts:
  - [ ] After selecting a side, typing `1`, `2`, `$5`, `10 usd`, etc. executes that trade directly (min $1).
- [ ] Closing positions:
  - [ ] `/portfolio` shows positions with `Close #X (full)` buttons.
  - [ ] Tapping Close calls `bot_tools.execute_sell_position` to SELL full size.

### 8. Gasless relay (design outline / TODO)

Planned integration with Polymarket **gasless relayer** ([docs](https://docs.polymarket.com/trading/gasless)):

- [ ] Add Python dependencies:
  - [ ] `py-builder-relayer-client`
  - [ ] `py-builder-signing-sdk`
- [ ] Create a shared relay module (e.g. `relay_client.py`):
  - [ ] Initialize `BuilderConfig` with env creds.
  - [ ] Expose helpers:
    - [ ] `get_relay_client(private_key: str) -> RelayClient`
    - [ ] `relay_trade(...)` – build and execute gasless trades.
    - [ ] `relay_approve_usdc(...)` – send USDC/CTF approvals via relayer (6‑tx pattern, but batched where possible).
    - [ ] `relay_redeem_positions(...)` – call `redeemPositions` for winning tokens.
- [ ] Update `/approve` and trading paths to use relay instead of direct web3 when relay is configured.

### 9. Future enhancements (copy any Polymarket user)

- [ ] Add support for following **external Polymarket addresses**:
  - [ ] Extend `copy_trading_hooks` to store `leader_address` in `config` when there is no local `leader_user_id`.
  - [ ] Background worker or webhook listener:
    - [ ] Polls Polymarket Data API for new trades by tracked addresses.
    - [ ] For each new trade:
      - [ ] Applies per‑hook modifiers (size multiplier, max daily volume, slippage caps).
      - [ ] Executes via gasless relay for followers.
- [ ] Sorting / pagination:
  - [ ] Use Data API leaderboards to build a **global** leaderboard of addresses by PnL/volume.
  - [ ] Combine with local leaderboard for app‑specific stats.

This checklist should give you a single place to see what’s already wired up in the repo and what pieces to connect next (notably the gasless relay and external‑address copy‑trading worker). Adjust or extend it as your integration evolves.

