# 🤖 HeyAnna - Polymarket Trading Bot

HeyAnna is a Telegram-first Polymarket assistant with:

- menu-driven market discovery and trading
- AI market analysis with live news context
- copy-trading workflows (follow leader wallets)
- gasless wallet flows (Safe + relayer)
- FastAPI endpoints for integrations and dashboards

---

## 🚀 Current Highlights

- **Market UX**: Banner-based market cards, sorting (trending/volume/closing), deep-link trade actions.
- **Trade Execution**: CLOB buy/sell with retry logic around approval/balance edge cases.
- **Portfolio/Wallet**: Unified account view with deposit, transfer, withdraw, claim.
- **Copy Trading**: Follow global wallets, quick risk presets, sizing mode selection.
- **Bridge Support**: Polymarket bridge deposit addresses and supported assets endpoints.
- **Private Key Protection**: Optional DB at-rest encryption for stored private keys.

---

## 🧱 Tech Stack

- **Bot**: `python-telegram-bot`
- **API**: `FastAPI` + `uvicorn`
- **Trading**: `py-clob-client`, `web3.py`
- **Gasless**: `py-builder-relayer-client`, `py-builder-signing-sdk`
- **LLM**: `langchain-openai` (OpenAI-compatible endpoint support)
- **Database**: SQLite via `DatabaseManager`
- **Tooling**: `uv`

---

## ⚙️ Quick Start

1. **Install deps**

```bash
uv sync
```

2. **Create env file**

```bash
cp .env.example .env
```

3. **Set minimum required vars**

- `BOT_TOKEN`
- `JWT_SECRET` (for API auth/session endpoints)
- `POLY_BUILDER_API_KEY`, `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE` (if using gasless flows)

4. **Run bot + API**

```bash
uv run python main.py
```

---

## 🔐 DB Private Key Encryption (Audit Mode)

The app can encrypt `eth_private_key` and `sol_private_key` at rest in SQLite.

Use these env flags:

- `DB_ENCRYPTION_ENABLED=1` -> enable encryption/decryption path
- `DB_ENCRYPTION_REQUIRED=1` -> fail-closed on misconfiguration
- `DB_ENCRYPTION_KEY` (preferred) or `PRIVATE_KEY_ENCRYPTION_KEY`
- `DB_ENCRYPTION_SALT` (recommended per environment)

Recommended production profile:

```env
DB_ENCRYPTION_ENABLED=1
DB_ENCRYPTION_REQUIRED=1
DB_ENCRYPTION_KEY=replace_with_strong_secret_or_fernet_key
DB_ENCRYPTION_SALT=replace_with_env_specific_salt
```

---

## 🖼️ Assets

Bot banners are now repo-local under:

- `assets/`

`bot_app.py` references them via local paths, so deployments are portable.

---

## 📋 Telegram Commands

- `/start`
- `/menu`
- `/markets`
- `/category`
- `/portfolio`
- `/wallet`
- `/balance`
- `/copy`
- `/follow <wallet>`
- `/approve`
- `/swap`
- `/close`
- `/help`

---

## 🛡️ Security Notes

- Never commit `.env` or raw secrets.
- Use DB encryption flags in production.
- Rotate encryption key material with a migration plan.
- Keep relayer credentials server-side only.
