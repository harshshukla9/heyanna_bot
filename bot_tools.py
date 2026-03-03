import logging
import requests
from web3 import Web3
import os
import json
from dotenv import load_dotenv
import market_cache

from py_clob_client.client import ClobClient
from mcp.server.fastmcp import FastMCP

# Ensure we load env vars so we can get the DFLOW_API_KEY
load_dotenv()
DFLOW_API_KEY = os.getenv("DFLOW_API_KEY", "")

# Connect to public RPC nodes
# In production, these should be Infura/Alchemy or similar API keys.
polygon_rpc_url = os.getenv("POLYGON_RPC_URL", "https://rpc.ankr.com/polygon")
poly_w3 = Web3(Web3.HTTPProvider(polygon_rpc_url))

# Initialize read-only Polymarket CLOB client
clob_client = ClobClient("https://clob.polymarket.com")

# Initialize FastMCP Server for our Tools
mcp = FastMCP("Anna")

@mcp.tool()
def get_eth_balance(address: str) -> str:
    """Get the current Ethereum (ETH) balance of a wallet address."""
    try:
        checksum_address = Web3.to_checksum_address(address)
        balance_wei = eth_w3.eth.get_balance(checksum_address)
        balance_eth = eth_w3.from_wei(balance_wei, 'ether')
        return f"{balance_eth:.4f} ETH"
    except Exception as e:
        logging.error(f"Error fetching ETH balance: {e}")
        return "Error fetching ETH balance"

@mcp.tool()
def get_polygon_balance(address: str) -> str:
    """Get the full portfolio balance of a Polygon wallet across all major verified tokens, returned in USD."""
    try:
        checksum_address = Web3.to_checksum_address(address)
        
        # ERC-20 balanceOf ABI (minimal)
        ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]')
        
        # Major verified Polygon tokens: (name, contract_address, decimals, coingecko_id)
        TOKENS = [
            ("USDC",  "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6,  "usd-coin"),
            ("USDT",  "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6,  "tether"),
            ("WETH",  "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", 18, "weth"),
            ("WBTC",  "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", 8,  "wrapped-bitcoin"),
            ("DAI",   "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", 18, "dai"),
            ("LINK",  "0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", 18, "chainlink"),
            ("AAVE",  "0xD6DF932A45C0f255f85145f286eA0b292B21C90B", 18, "aave"),
            ("UNI",   "0xb33EaAd8d922B1083446DC23f610c2567fB5180f", 18, "uniswap"),
            ("POL",   "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", 18, "polygon-ecosystem-token"),
        ]
        
        # 1. Get native POL balance
        native_wei = poly_w3.eth.get_balance(checksum_address)
        native_bal = float(poly_w3.from_wei(native_wei, 'ether'))
        
        balances = {"POL (native)": native_bal}
        coingecko_ids = ["polygon-ecosystem-token"]
        
        # 2. Get ERC-20 token balances
        for name, contract_addr, decimals, cg_id in TOKENS:
            try:
                contract = poly_w3.eth.contract(
                    address=Web3.to_checksum_address(contract_addr),
                    abi=ERC20_ABI
                )
                raw_balance = contract.functions.balanceOf(checksum_address).call()
                token_bal = raw_balance / (10 ** decimals)
                if token_bal > 0:
                    balances[name] = token_bal
                    if cg_id not in coingecko_ids:
                        coingecko_ids.append(cg_id)
            except Exception:
                pass
        
        # 3. Fetch USD prices from CoinGecko (free, no API key)
        prices = {}
        try:
            ids_str = ",".join(coingecko_ids)
            price_resp = requests.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd",
                timeout=10
            )
            if price_resp.status_code == 200:
                prices = price_resp.json()
        except Exception:
            pass
        
        # Map token names to coingecko IDs for price lookup
        name_to_cg = {"POL (native)": "polygon-ecosystem-token"}
        for name, _, _, cg_id in TOKENS:
            name_to_cg[name] = cg_id
        
        # 4. Build the output
        lines = ["📊 **Polygon Wallet Portfolio**\n"]
        total_usd = 0.0
        
        for token_name, token_bal in balances.items():
            cg_id = name_to_cg.get(token_name, "")
            usd_price = prices.get(cg_id, {}).get("usd", 0)
            token_usd = token_bal * usd_price
            total_usd += token_usd
            
            if token_bal > 0:
                lines.append(f"  • {token_name}: {token_bal:.4f} (${token_usd:.2f})")
        
        lines.append(f"\n💰 **Total: ${total_usd:.2f} USD**")
        
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"Error fetching Polygon balance: {e}")
        return f"Error fetching Polygon balance: {str(e)}"

# Polymarket contract addresses on Polygon (from official gist)
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
MAX_APPROVAL = 2**256 - 1

ERC20_APPROVE_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]')
ERC1155_APPROVAL_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"operator","type":"address"},{"internalType":"bool","name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]')


@mcp.tool()
def approve_usdc_for_trading(address: str) -> str:
    """Set all USDC + CTF allowances for Polymarket trading (6 on-chain transactions). Must be called once before first trade."""
    import database
    from web3 import Web3
    
    db_user = database.get_user_by_address(address)
    if not db_user:
        return "Could not find wallet for this address."
    
    private_key = db_user["eth_private_key"]
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    account = w3.eth.account.from_key(private_key)
    
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_POLYGON), abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_APPROVAL_ABI)
    
    results = []
    
    # Official Polymarket allowance pattern: 6 transactions
    # For each spender: approve USDC + setApprovalForAll on CTF
    spenders = [
        ("CTF Exchange", EXCHANGE_ADDRESS),
        ("NegRisk CTF Exchange", NEG_RISK_EXCHANGE),
        ("NegRisk Adapter", NEG_RISK_ADAPTER),
    ]
    
    for name, spender in spenders:
        # 1. USDC approve
        try:
            nonce = w3.eth.get_transaction_count(account.address)
            tx = usdc.functions.approve(
                Web3.to_checksum_address(spender), MAX_APPROVAL
            ).build_transaction({"chainId": 137, "from": account.address, "nonce": nonce})
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logging.info(f"[Approve] USDC → {name} TX: {tx_hash.hex()}")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            results.append(f"✅ USDC → {name}: {tx_hash.hex()}")
        except Exception as e:
            logging.error(f"[Approve] USDC → {name} FAILED: {e}")
            results.append(f"❌ USDC → {name}: {str(e)}")
        
        # 2. CTF setApprovalForAll
        try:
            nonce = w3.eth.get_transaction_count(account.address)
            tx = ctf.functions.setApprovalForAll(
                Web3.to_checksum_address(spender), True
            ).build_transaction({"chainId": 137, "from": account.address, "nonce": nonce})
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logging.info(f"[Approve] CTF → {name} TX: {tx_hash.hex()}")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            results.append(f"✅ CTF → {name}: {tx_hash.hex()}")
        except Exception as e:
            logging.error(f"[Approve] CTF → {name} FAILED: {e}")
            results.append(f"❌ CTF → {name}: {str(e)}")
    
    return "Polymarket Allowance Transactions (6 total):\n" + "\n".join(results)


@mcp.tool()
def get_polymarket_portfolio(address: str) -> str:
    """Get a complete overview of the user's Polymarket portfolio, including funds and open positions."""
    import requests
    
    # 1. Get on-chain balances
    on_chain_summary = get_polygon_balance(address)
    
    # 2. Get open positions from Data API
    try:
        url = f"https://data-api.polymarket.com/positions?user={address}"
        resp = requests.get(url, timeout=10)
        positions = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        logging.error(f"Error fetching portfolio positions: {e}")
        positions = []
    
    lines = [
        "📊 **POLYMARKET PORTFOLIO OVERVIEW**",
        f"Wallet: `{address}`",
        "\n💰 **On-Chain Funds**",
        on_chain_summary.split("\n", 2)[-1] if "\n" in on_chain_summary else on_chain_summary
    ]
    
    if not positions:
        lines.append("\n📈 **Open Positions**: None")
    else:
        lines.append(f"\n📈 **Open Positions ({len(positions)})**")
        total_pnl = 0.0
        portfolio_value = 0.0
        
        for p in positions:
            title = p.get("title", "Unknown Market")
            outcome = p.get("outcome", "Unknown")
            size = float(p.get("size", 0))
            avg_price = float(p.get("avgPrice", 0))
            cur_price = float(p.get("curPrice", 0))
            cur_val = float(p.get("currentValue", 0))
            pnl_pct = float(p.get("percentPnl", 0))
            
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            
            lines.append(
                f"• **{title}**\n"
                f"  Side: {outcome} | Size: {size:.2f} shares\n"
                f"  Buy: {avg_price*100:.1f}¢ | Now: {cur_price*100:.1f}¢ | Value: ${cur_val:.2f}\n"
                f"  PnL: {pnl_emoji} {pnl_pct:+.2f}%"
            )
            total_pnl += float(p.get("cashPnl", 0))
            portfolio_value += cur_val

        lines.append(f"\n💵 **Total Portfolio Value: ${portfolio_value:.2f}**")
        lines.append(f"{'🟢' if total_pnl >= 0 else '🔴'} **Total PnL: ${total_pnl:+.2f}**")
    
    return "\n".join(lines)


# Uniswap V3 SwapRouter on Polygon
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC.e (native Circle USDC)

SWAP_ROUTER_ABI = json.loads("""[
    {"inputs":[{"components":[
        {"name":"tokenIn","type":"address"},
        {"name":"tokenOut","type":"address"},
        {"name":"fee","type":"uint24"},
        {"name":"recipient","type":"address"},
        {"name":"deadline","type":"uint256"},
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"},
        {"name":"sqrtPriceLimitX96","type":"uint160"}
    ],"name":"params","type":"tuple"}],
    "name":"exactInputSingle",
    "outputs":[{"name":"amountOut","type":"uint256"}],
    "stateMutability":"payable","type":"function"}
]""")


@mcp.tool()
def swap_usdc_for_trading(address: str, amount: str = "all") -> str:
    """Swap native USDC.e to Polymarket-compatible bridged USDC via Uniswap V3. Amount in USD or 'all' for full balance."""
    import database
    from web3 import Web3
    import time
    
    db_user = database.get_user_by_address(address)
    if not db_user:
        return "Could not find wallet for this address."
    
    private_key = db_user["eth_private_key"]
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    account = w3.eth.account.from_key(private_key)
    
    # Check native USDC.e balance
    usdc_native = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_NATIVE),
        abi=ERC20_APPROVE_ABI + json.loads('[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]')
    )
    
    balance = usdc_native.functions.balanceOf(account.address).call()
    if balance == 0:
        return "No native USDC.e found in wallet to swap."
    
    if amount == "all":
        swap_amount = balance
    else:
        swap_amount = int(float(amount) * 1e6)  # USDC has 6 decimals
        if swap_amount > balance:
            swap_amount = balance
    
    try:
        # Step 1: Approve Uniswap router to spend USDC.e
        nonce = w3.eth.get_transaction_count(account.address)
        approve_tx = usdc_native.functions.approve(
            Web3.to_checksum_address(UNISWAP_V3_ROUTER),
            swap_amount
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 60000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        })
        signed_approve = account.sign_transaction(approve_tx)
        approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        logging.info(f"[Swap] USDC.e approve TX: {approve_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)
        
        # Step 2: Swap USDC.e → bridged USDC via Uniswap V3
        router = w3.eth.contract(
            address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
            abi=SWAP_ROUTER_ABI,
        )
        
        swap_params = (
            Web3.to_checksum_address(USDC_NATIVE),      # tokenIn (native USDC.e)
            Web3.to_checksum_address(USDC_POLYGON),      # tokenOut (bridged USDC)
            100,                                          # fee tier 0.01% (stablecoin pool)
            account.address,                              # recipient
            int(time.time()) + 600,                       # deadline (10 min)
            swap_amount,                                  # amountIn
            int(swap_amount * 0.995),                     # amountOutMinimum (0.5% slippage)
            0,                                            # sqrtPriceLimitX96
        )
        
        swap_tx = router.functions.exactInputSingle(swap_params).build_transaction({
            "from": account.address,
            "nonce": nonce + 1,
            "gas": 200000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
            "value": 0,
        })
        
        signed_swap = account.sign_transaction(swap_tx)
        swap_hash = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
        logging.info(f"[Swap] USDC.e → bridged USDC TX: {swap_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(swap_hash, timeout=30)
        
        status = "✅ SUCCESS" if receipt["status"] == 1 else "❌ FAILED"
        return (
            f"{status}\n"
            f"Swapped {swap_amount / 1e6} USDC.e → bridged USDC\n"
            f"TX: {swap_hash.hex()}\n"
            f"Now run 'approve USDC for trading' before your first trade."
        )
        
    except Exception as e:
        logging.error(f"USDC swap failed: {e}")
        return f"❌ Swap failed: {str(e)}"

def _fetch_and_cache_events(url_params: dict, query_filter: str = None, header: str = "Active Markets") -> str:
    """Shared logic: fetch events from Gamma, populate market_cache, return formatted text."""
    from datetime import datetime, timezone
    
    url = "https://gamma-api.polymarket.com/events/pagination"
    response = requests.get(url, params=url_params)
    
    if response.status_code != 200:
        return "Error fetching markets from Polymarket API."
    
    data = response.json()
    events = data.get("data", [])
    if not events:
        return "No active markets found on Polymarket right now."
    
    now = datetime.now(timezone.utc)
    market_cache.clear()
    
    for event in events:
        end_date_str = event.get("endDate", "")
        if end_date_str:
            try:
                if end_date_str.endswith("Z"):
                    end_date_str = end_date_str[:-1] + "+00:00"
                end_date = datetime.fromisoformat(end_date_str)
                if end_date < now:
                    continue
            except Exception:
                pass
        
        title = event.get("title", "No Title")
        
        if query_filter and query_filter.lower() not in title.lower():
            continue
        
        for market in event.get("markets", [])[:5]:
            m_question = market.get("question", "")
            condition_id = market.get("conditionId", "")
            outcomes = market.get("outcomes", [])
            tokens_raw = market.get("clobTokenIds", [])
            
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(tokens_raw, str):
                tokens_raw = json.loads(tokens_raw)
            
            if not outcomes or not tokens_raw or len(outcomes) != len(tokens_raw):
                continue
            
            # Fetch live odds via direct CLOB HTTP API
            odds = {}
            for o_name, t_id in zip(outcomes, tokens_raw):
                try:
                    mid_resp = requests.get(
                        f"https://clob.polymarket.com/midpoint?token_id={t_id}",
                        timeout=5
                    )
                    if mid_resp.status_code == 200:
                        mid_data = mid_resp.json()
                        mid_val = float(mid_data.get("mid", 0))
                        odds[o_name] = int(mid_val * 100)
                    else:
                        odds[o_name] = 0
                except Exception:
                    odds[o_name] = 0
            
            market_cache.add(
                question=m_question,
                event_title=title,
                condition_id=condition_id,
                outcomes=list(outcomes),
                clob_token_ids=list(tokens_raw),
                odds=odds,
                end_date=end_date_str,
            )
        
        # Stop after caching 20 markets to keep output manageable
        if len(market_cache.list_all()) >= 20:
            break
    
    return market_cache.format_all()


@mcp.tool()
def get_polymarket_markets() -> str:
    """Fetch trending open prediction markets from Polymarket. Each market gets a #ID you can use for trading."""
    try:
        return _fetch_and_cache_events(
            url_params={
                "limit": 20, "active": "true", "archived": "false",
                "closed": "false", "order": "volume24hr",
                "ascending": "false", "offset": 20
            },
            header="Trending Markets"
        )
    except Exception as e:
        logging.error(f"Error fetching Polymarket markets: {e}")
        return "Error fetching Polymarket markets."

@mcp.tool()
def search_polymarket_events(query: str) -> str:
    """Search for specific active prediction markets by keyword. Each market gets a #ID you can use for trading."""
    try:
        return _fetch_and_cache_events(
            url_params={
                "limit": 100, "active": "true", "archived": "false",
                "closed": "false", "order": "volume24hr",
                "ascending": "false", "offset": 0
            },
            query_filter=query,
            header=f"Markets matching '{query}'"
        )
    except Exception as e:
        logging.error(f"Error searching Polymarket markets: {e}")
        return "Error searching Polymarket markets."


@mcp.tool()
def get_market_by_id(market_id: int) -> str:
    """Look up full details of a cached market by its #ID number. Returns question, odds, condition_id, and token IDs."""
    m = market_cache.get(market_id)
    if not m:
        return f"Market #{market_id} not found. Use get_polymarket_markets or search_polymarket_events first to load markets."
    
    odds_parts = [f"{o}: {m.odds.get(o, '?')}¢" for o in m.outcomes]
    token_parts = [f"{o}: {tid}" for o, tid in zip(m.outcomes, m.clob_token_ids)]
    
    return (
        f"Market #{m.market_id}\n"
        f"Question: {m.question}\n"
        f"Event: {m.event_title}\n"
        f"Condition ID: {m.condition_id}\n"
        f"Odds: {' | '.join(odds_parts)}\n"
        f"Token IDs: {' | '.join(token_parts)}\n"
        f"End Date: {m.end_date}"
    )


@mcp.tool()
def execute_trade(market_id: int, side: str, amount: str, address: str) -> str:
    """Execute a REAL trade on Polymarket. Use the #ID from the market list. side must be 'Yes' or 'No'. amount is in USD."""
    import database
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    
    # 1. Resolve market from cache
    m = market_cache.get(market_id)
    if not m:
        return f"Market #{market_id} not found in cache. Use get_polymarket_markets or search_polymarket_events first."
    
    side = side.strip().capitalize()
    if side not in m.outcomes:
        return f"Invalid side '{side}'. Available outcomes: {', '.join(m.outcomes)}"
    
    # 2. Resolve the CLOB token ID for the chosen side
    side_idx = m.outcomes.index(side)
    token_id = m.clob_token_ids[side_idx]
    odds_cents = m.odds.get(side, 0)
    
    # 3. Get user's private key
    db_user = database.get_user_by_address(address)
    if not db_user:
        return "CRITICAL: Could not find user private key for this wallet address."
    
    private_key = db_user["eth_private_key"]
    
    try:
        # 4. Create L1 client to derive API credentials
        l1_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
        )
        creds = l1_client.create_or_derive_api_creds()
        
        # 5. Create L2 client with creds in constructor (per official docs)
        trade_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
            signature_type=0,  # 0 = EOA wallet
        )
        
        # 6. Build and sign the market order
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount),
            side="BUY",
        )
        signed_order = trade_client.create_market_order(order_args)
        
        # 7. Post the order (Fill-Or-Kill)
        resp = trade_client.post_order(signed_order, orderType=OrderType.FOK)
        
        # 7. Extract order details from response
        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("id", "N/A"))
            status = resp.get("status", "submitted")
            tx_hash = resp.get("transactHash", resp.get("txHash", resp.get("transactionHash", "pending")))
        else:
            order_id = str(resp)
            status = "submitted"
            tx_hash = "pending"
        
        return (
            f"✅ TRADE EXECUTED\n"
            f"Market: {m.question}\n"
            f"Side: {side} @ {odds_cents}¢\n"
            f"Amount: ${amount}\n"
            f"Order ID: {order_id}\n"
            f"Status: {status}\n"
            f"TX Hash: {tx_hash}\n"
            f"Token ID: {token_id}"
        )
        
    except Exception as e:
        logging.error(f"Trade execution failed: {e}")
        return f"❌ TRADE FAILED: {str(e)}"

@mcp.tool()
def search_news(query: str, max_results: int = 5) -> str:
    """Search for the latest global news articles about a specific topic to help forecast prediction market odds. Returns headlines, snippets, and publication dates via DuckDuckGo."""
    try:
        from ddgs import DDGS
        
        results = []
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "date": r.get("date", ""),
                    "title": r.get("title", ""),
                    "source": r.get("source", ""),
                    "snippet": r.get("body", "")
                })
                
        if not results:
            return f"No recent news found for query: '{query}'"
            
        return json.dumps(results, indent=2)
    except Exception as e:
        logging.error(f"Error fetching news for {query}: {e}")
        return f"Failed to fetch news. Error: {str(e)}"
