import os
import json
import logging
import asyncio
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

import bot_tools

load_dotenv()

# -- LLM Configuration --
llm = ChatOpenAI(
    base_url="https://llm.adityaberry.me/v1",
    api_key=os.getenv("OPENAI_API_KEY", "dummy-key"),
    model="Qwen3-Coder-Next-UD-Q4_K_XL.gguf",
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


async def get_chat_response_stream(messages: list):
    """
    LangGraph-powered agentic orchestrator.
    
    Runs the full ReAct agent loop to completion (tool calls happen internally),
    then yields only the final clean answer to the user.
    """
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
    
    # Convert OpenAI message format → LangChain message format
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
        # Run the full agent to completion — all tool calls happen internally
        result = await agent.ainvoke(
            {"messages": lc_messages},
            config={"recursion_limit": 20},
        )
        
        # Extract the final AI message from the result
        final_messages = result.get("messages", [])
        
        # Walk backwards to find the last AIMessage with content and no tool_calls
        final_text = ""
        for msg in reversed(final_messages):
            if isinstance(msg, AIMessage) and msg.content and not getattr(msg, 'tool_calls', None):
                final_text = msg.content
                break
        
        if final_text:
            yield {"type": "content", "data": final_text}
        else:
            yield {"type": "content", "data": "I processed your request but couldn't generate a final response. Please try again."}
                    
    except Exception as e:
        logging.error(f"LangGraph agent error: {e}")
        yield {"type": "content", "data": f"Sorry, I encountered an error: {str(e)}"}
