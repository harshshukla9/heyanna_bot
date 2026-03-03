# 🤖 HeyAnna - Polymarket AI Trading Bot

**HeyAnna** is a high-performance Telegram bot designed for seamless interaction with **Polymarket**. It combines local LLM reasoning (via LangGraph) with real-time on-chain execution, allowing users to discover, analyze, and trade prediction markets through natural language or explicit commands.

---

## 🚀 Features

### 🧠 Intelligent Trading (Anna)
- **Superforecaster AI**: Anna uses a local `Qwen3` model to analyze news context and market odds before recommending trades.
- **Natural Language Execution**: "Trade $10 on Yes for market #4" or "What do you think about the Iran regime fall market?".
- **News Grounding**: Integrated with DuckDuckGo Search to fetch real-time context for accurate market forecasting.

### 📈 Polymarket Integration
- **Market Discovery**: Real-time trending events and semantic search across the Gamma API.
- **CLOB Execution**: Direct trade execution on the Polymarket Central Limit Order Book (CLOB) via `py-clob-client`.
- **Market Registry**: A global cache that maps simple shorthand IDs (`#1`, `#2`) to complex contract addresses for easy user interaction.

### 💰 Portfolio & Wallet Management
- **Consolidated Portfolio**: Unified view of on-chain funds (POL, USDC, USDC.e) and active Polymarket positions with real-time PnL tracking.
- **One-Click Setup**: 
  - `/swap`: Converts native Polygon USDC.e to Polymarket-compatible bridged USDC.
  - `/approve`: Executes the official 6-transaction approval flow for Polymarket Exchange, NegRisk, and CTF contracts.
- **Self-Custodial**: Automatic generation of EVM (Polygon) wallets for every user, persisted in a local DuckDB instance.

---

## 🛠️ Technical Stack

- **Orchestration**: [LangGraph](https://github.com/langchain-ai/langgraph) / [LangChain](https://github.com/langchain-ai/langchain)
- **Trading Engine**: `py-clob-client`, `py-order-utils`
- **Blockchain**: `web3.py` (Polygon Mainnet)
- **Database**: `duckdb` (Local persistent storage)
- **Bot Framework**: `python-telegram-bot`
- **Tooling**: `uv` for package management

---

## 📋 Commands

| Command | Description |
| :--- | :--- |
| `/start` | Welcome & wallet initialization |
| `/portfolio` | Full breakdown of funds + active market positions |
| `/markets` | View trending Polymarket events |
| `/trending` | Alias for /markets |
| `/balance` | Quick check of on-chain assets |
| `/swap` | Prepare funds for trading (USDC.e → USDC) |
| `/approve` | Setup all required contract permissions |
| `/wallet` | Show your Polygon address |
| `/help` | List available tools |

---

## ⚙️ Setup & Installation

1. **Install Dependencies**:
   ```bash
   uv sync
   ```

2. **Configure Environment (`.env`)**:
   ```env
   BOT_TOKEN=your_telegram_bot_token
   POLYGON_RPC_URL=https://polygon-rpc.com
   OPENAI_API_KEY=your_token_if_using_remote_llm
   TAVILY_API_KEY=your_key
   ```

3. **Run the Bot**:
   ```bash
   uv run python bot.py
   ```

---

## 🔒 Security Note
Private keys are handled with care and only used for signing on-chain transactions or deriving CLOB API credentials. Ensure your `.env` and `bot_data.duckdb` are secured.

---
*Created with ❤️ aditya*
