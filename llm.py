import os
import json
import logging
import asyncio
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

import bot_tools

load_dotenv()

# -- LLM Configuration --
# OpenRouter: OpenAI-compatible API with access to many models including free tiers.
llm = ChatOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY", os.getenv("OPENAI_API_KEY", "")),
    model="nvidia/nemotron-3-super-120b-a12b:free",
    temperature=0.7,
    max_tokens=8096,
    streaming=True,
)


# ── Bridge FastMCP tools into LangChain @tool functions (async) ──

@tool
async def get_polygon_balance(address: str) -> str:
    """Get the full portfolio balance of a Polygon wallet across all major verified tokens, returned in USD."""
    return await _call_mcp("get_polygon_balance", {"address": address})

@tool
async def get_eth_balance(address: str) -> str:
    """Get the current Ethereum (ETH) balance of a wallet address."""
    return await _call_mcp("get_eth_balance", {"address": address})

@tool
async def get_polymarket_markets() -> str:
    """Fetch the trending open prediction markets from Polymarket with real-time odds."""
    return await _call_mcp("get_polymarket_markets", {})

@tool
async def search_polymarket_events(query: str) -> str:
    """Search for specific active prediction markets by keyword and get real odds from CLOB."""
    return await _call_mcp("search_polymarket_events", {"query": query})

@tool
async def get_polymarket_markets_by_category(category: str) -> str:
    """Fetch Polymarket markets filtered by category (tag slug, e.g. politics, crypto, finance). Each market gets a #ID for trading."""
    return await _call_mcp("get_polymarket_markets_by_category", {"category": category})

@tool
async def search_news(query: str, max_results: int = 5) -> str:
    """Search for the latest global news articles about a specific topic via DuckDuckGo to help forecast prediction market odds."""
    return await _call_mcp("search_news", {"query": query, "max_results": max_results})

@tool
async def get_kalshi_markets() -> str:
    """Fetch active prediction events from Kalshi (via DFlow API)."""
    return await _call_mcp("get_kalshi_markets", {})

@tool
async def execute_trade(market_id: int, side: str, amount: str, address: str) -> str:
    """Execute a trade on a prediction market. Use the #ID from the market list. side must be 'Yes' or 'No'. amount is in USD."""
    return await _call_mcp("execute_trade", {
        "market_id": market_id, "side": side,
        "amount": amount, "address": address
    })

@tool
async def get_market_by_id(market_id: int) -> str:
    """Look up full details of a cached market by its #ID number. Returns question, odds, condition_id, and token IDs."""
    return await _call_mcp("get_market_by_id", {"market_id": market_id})


async def _call_mcp(name: str, args: dict) -> str:
    """Helper to call an MCP tool and extract the text result."""
    try:
        result = await bot_tools.mcp.call_tool(name, args)
        return "\n".join(c.text for c in result[0] if c.type == 'text')
    except Exception as e:
        logging.error(f"MCP tool {name} failed: {e}")
        return f"Error: {e}"

@tool
async def approve_usdc_for_trading(address: str) -> str:
    """Approve USDC spending for Polymarket exchange contracts. Must be called once before a user's first trade."""
    return await _call_mcp("approve_usdc_for_trading", {"address": address})

@tool
async def swap_usdc_for_trading(address: str, amount: str = "all") -> str:
    """Swap native USDC.e to Polymarket-compatible bridged USDC. Amount in USD or 'all' for full balance."""
    return await _call_mcp("swap_usdc_for_trading", {"address": address, "amount": amount})

@tool
async def get_polymarket_portfolio(address: str) -> str:
    """Get a complete overview of the user's Polymarket portfolio, including funds and open positions."""
    return await _call_mcp("get_polymarket_portfolio", {"address": address})


# All tools available to the agent
ALL_TOOLS = [
    get_polygon_balance,
    get_eth_balance,
    get_polymarket_markets,
    search_polymarket_events,
    get_polymarket_markets_by_category,
    search_news,
    get_kalshi_markets,
    execute_trade,
    get_market_by_id,
    approve_usdc_for_trading,
    swap_usdc_for_trading,
    get_polymarket_portfolio,
]


def _build_agent():
    """Build a LangGraph ReAct agent with all tools."""
    return create_react_agent(
        model=llm,
        tools=ALL_TOOLS,
    )


def _format_tool_call(name: str, args: dict) -> str:
    """Format tool call for display (compact)."""
    import json
    try:
        args_str = json.dumps(args, ensure_ascii=False)[:80]
        if len(json.dumps(args)) > 80:
            args_str += "..."
    except Exception:
        args_str = "..."
    return f"→ {name}({args_str})"


def _truncate_result(text: str, max_len: int = 120) -> str:
    """Truncate tool result for display."""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


async def get_chat_response_stream(messages: list):
    """
    Stream real LLM tokens and tool call logs from the LangGraph ReAct agent.
    Yields: {"type": "tool_call", "data": "→ get_polymarket_markets()..."}
            {"type": "tool_result", "data": "← get_polymarket_markets() returned: ..."}
            {"type": "content", "data": token}
    """
    lc_messages = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if role == "system":
            lc_messages.append(SystemMessage(content=content))
        elif role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))

    agent = _build_agent()

    try:
        async for event in agent.astream(
            {"messages": lc_messages},
            config={"recursion_limit": 20},
            stream_mode=["updates", "messages"],
        ):
            # event is (mode, data) when stream_mode is a list
            if isinstance(event, tuple) and len(event) == 2:
                mode, data = event
            else:
                continue

            if mode == "updates":
                for node_name, update in (data or {}).items():
                    if node_name == "agent":
                        msgs = update.get("messages", [])
                        for m in msgs:
                            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                                for tc in m.tool_calls:
                                    name = tc.get("name", "?")
                                    args = {}
                                    try:
                                        import json
                                        raw = tc.get("args") or tc.get("function", {}).get("arguments", "{}")
                                        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                                    except Exception:
                                        pass
                                    yield {"type": "tool_call", "data": _format_tool_call(name, args)}
                    elif node_name == "tools":
                        msgs = update.get("messages", [])
                        for m in msgs:
                            if isinstance(m, ToolMessage):
                                name = getattr(m, "name", "tool")
                                raw = getattr(m, "content", "") or ""
                                content = raw if isinstance(raw, str) else "\n".join(str(x) for x in (raw or []))
                                line = f"← {name} returned: {_truncate_result(content)}"
                                yield {"type": "tool_result", "data": line}

            elif mode == "messages":
                chunk, meta = data if isinstance(data, tuple) else (data, {})
                if chunk and getattr(chunk, "content", None):
                    yield {"type": "content", "data": chunk.content}

    except Exception as e:
        logging.error(f"LangGraph agent error: {e}")
        yield {"type": "content", "data": f"Sorry, I encountered an error: {str(e)}"}


async def get_chat_response(messages: list) -> str:
    """
    Convenience helper: run the LangGraph agent once and return the final
    assistant content as a single string (no streaming).
    Internally reuses get_chat_response_stream and concatenates content chunks.
    """
    content = ""
    async for chunk in get_chat_response_stream(messages):
        if chunk.get("type") == "content":
            content += chunk.get("data", "")
    return (content or "Sorry, I had nothing to say.").strip()


async def run_market_analysis(query: str) -> str:
    """
    One-shot market analysis helper for the HTTP API.

    IMPORTANT: This path bypasses the LangGraph tools/agent and talks directly
    to the OpenAI-compatible chat completion endpoint. This avoids issues with
    more complex tool-calling payloads that some gateways reject.

    The model is instructed to:
    - use the provided DuckDuckGo news context,
    - reason about the event and likely market pricing,
    - output a probability and 0–100 risk score.
    """
    # 1) Fetch news via multiple focused queries using the bot_tools.search_news MCP tool.
    # This gives the model a broader, more complete picture than a single query.
    subqueries = [
        query,
        f"{query} latest",
        f"{query} protests",
        f"{query} diplomacy",
        f"{query} war",
        f"{query} sanctions",
    ]
    aggregated: list[dict] = []
    seen_keys: set[tuple] = set()

    for sq in subqueries:
        try:
            raw = await asyncio.to_thread(bot_tools.search_news, sq, 5)
        except Exception as e:
            logging.error(f"run_market_analysis news fetch error for '{sq}': {e}")
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            key = (item.get("title", ""), item.get("date", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            aggregated.append(item)

        if len(aggregated) >= 20:
            break

    # 2) Turn aggregated news into a compact bullet list for the model
    news_block = ""
    if aggregated:
        # Sort newest first if dates look comparable; otherwise keep insertion order
        try:
            aggregated.sort(key=lambda x: x.get("date", ""), reverse=True)
        except Exception:
            pass
        lines: list[str] = []
        for item in aggregated[:15]:
            date = item.get("date", "") or ""
            title = item.get("title", "") or ""
            source = item.get("source", "") or ""
            snippet = item.get("snippet", "") or ""
            prefix = f"- [{date}] " if date else "- "
            meta = f" ({source})" if source else ""
            lines.append(f"{prefix}{title}{meta}: {snippet}")
        news_block = "\n".join(lines)
    else:
        news_block = f"No recent news found for: '{query}'"

    # ── Multi-agent style pipeline ──────────────────────────────────────────────
    # Agent 1: Researcher – cluster news into themes.
    researcher_system = SystemMessage(
        content=(
            "You are a research analyst. Your job is to read a block of recent news "
            "about a topic and break it into 2–4 key themes, each with a short label "
            "and 2–4 bullet points of evidence.\n\n"
            "Think step by step to yourself but ONLY output Markdown in this format:\n"
            "## Themes\n"
            "### Theme 1: <short label>\n"
            "- bullet\n"
            "- bullet\n"
            "### Theme 2: <short label>\n"
            "- bullet\n"
            "... (up to Theme 4)\n"
        )
    )
    researcher_user = HumanMessage(
        content=(
            f"USER QUERY:\n{query}\n\n"
            f"RECENT NEWS (DuckDuckGo):\n{news_block}\n"
        )
    )

    try:
        researcher_resp = await llm.ainvoke([researcher_system, researcher_user])
        themes_md = (getattr(researcher_resp, "content", "") or "").strip()
    except Exception as e:
        logging.error(f"run_market_analysis researcher error: {e}")
        themes_md = ""

    # Agent 2: Bullish forecaster – argue the YES case.
    bull_system = SystemMessage(
        content=(
            "You are a bullish forecaster arguing for the YES side of a prediction market "
            "(that the event WILL happen).\n"
            "Using the user query, themes, and news, you must build the strongest plausible case "
            "FOR the event happening.\n\n"
            "Think step by step to yourself but ONLY output Markdown in this format:\n"
            "## Bullish case (YES)\n"
            "- 3–5 bullets summarizing why the event might happen.\n"
            "Bullish probability: NN% (0–100, your best estimate that YES is correct).\n"
        )
    )
    bull_user = HumanMessage(
        content=(
            f"USER QUERY:\n{query}\n\n"
            f"THEMES:\n{themes_md}\n\n"
            f"RECENT NEWS (DuckDuckGo):\n{news_block}\n"
        )
    )

    try:
        bull_resp = await llm.ainvoke([bull_system, bull_user])
        bull_md = (getattr(bull_resp, "content", "") or "").strip()
    except Exception as e:
        logging.error(f"run_market_analysis bull error: {e}")
        bull_md = ""

    # Agent 3: Bearish forecaster – argue the NO case.
    bear_system = SystemMessage(
        content=(
            "You are a bearish forecaster arguing for the NO side of a prediction market "
            "(that the event will NOT happen).\n"
            "Using the user query, themes, and news, you must build the strongest plausible case "
            "AGAINST the event happening.\n\n"
            "Think step by step to yourself but ONLY output Markdown in this format:\n"
            "## Bearish case (NO)\n"
            "- 3–5 bullets summarizing why the event might NOT happen.\n"
            "Bearish probability: NN% (0–100, your best estimate that NO is correct).\n"
        )
    )
    bear_user = HumanMessage(
        content=(
            f"USER QUERY:\n{query}\n\n"
            f"THEMES:\n{themes_md}\n\n"
            f"RECENT NEWS (DuckDuckGo):\n{news_block}\n"
        )
    )

    try:
        bear_resp = await llm.ainvoke([bear_system, bear_user])
        bear_md = (getattr(bear_resp, "content", "") or "").strip()
    except Exception as e:
        logging.error(f"run_market_analysis bear error: {e}")
        bear_md = ""

    # Agent 4: Final synthesizer – combine everything into a single, crisp forecast.
    final_system = SystemMessage(
        content=(
            "You are Anna, a Polymarket-focused market analyst.\n\n"
            "You are given:\n"
            "1) A user query about an event or market.\n"
            "2) A compact list of recent news headlines/snippets from DuckDuckGo.\n"
            "3) A THEMES section produced by a researcher.\n"
            "4) A Bullish case (YES) and a Bearish case (NO).\n\n"
            "STRICT RULES:\n"
            "- Use ONLY this provided context plus robust background knowledge (no live browsing).\n"
            "- If legal or constitutional rules make an outcome impossible or nearly impossible "
            "(e.g., term limits, already resolved events), you MUST say so explicitly and set "
            "the probability near 0% for that side.\n"
            "- Do NOT invent precise polling numbers or odds that are not clearly implied by the news; "
            "keep numbers approximate unless directly stated.\n"
            "- First, reason step by step and weigh the bullish vs bearish evidence to yourself, "
            "but DO NOT show this internal reasoning. Then, write a short, clean answer in the format below.\n"
            "- Keep the answer crisp and short.\n\n"
            "You MUST produce Markdown with exactly these sections:\n"
            "## Summary\n"
            "- 3–5 bullet points summarizing the situation in plain language, referencing both bullish and bearish arguments.\n"
            "## Data points\n"
            "- 3–6 bullets highlighting concrete facts from the news block (dates, sources, key events).\n"
            "## Forecast & risk\n"
            "- 1–2 short sentences giving your overall view. If you say NO, explicitly say something like "
            "\"There is around a 70% chance the regime will *not* collapse\" (focus on the side you picked).\n"
            "- Then three separate lines in this exact format:\n"
            "  Verdict: YES or NO (your best binary call on whether the event will happen)\n"
            "  Implied odds: NN% (0–100, your estimated probability that your Verdict side is correct, rounded to a whole number)\n"
            "  Risk score: NN/100 (0–100, how volatile/uncertain the situation is)\n"
            "Follow this format exactly so the output is easy to parse."
        )
    )
    final_user = HumanMessage(
        content=(
            f"USER QUERY:\n{query}\n\n"
            f"RECENT NEWS (DuckDuckGo):\n{news_block}\n\n"
            f"{themes_md}\n\n"
            f"{bull_md}\n\n"
            f"{bear_md}\n"
        )
    )

    try:
        final_resp = await llm.ainvoke([final_system, final_user])
        text = getattr(final_resp, "content", "") or ""
        return text.strip() or "Sorry, I could not generate an analysis."
    except Exception as e:
        logging.error(f"run_market_analysis final synth error: {e}")
        return f"Sorry, I could not generate an analysis: {e}"
