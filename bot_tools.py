import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from web3 import Web3
import os
import json
from dotenv import load_dotenv
import market_cache
from database_manager import DatabaseManager

from py_clob_client.client import ClobClient
from mcp.server.fastmcp import FastMCP

try:
    # Polymarket Builder relayer client for gasless transactions.
    from py_builder_relayer_client.client import RelayClient
    # Builder signing config (matches Polymarket example)
    from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
    from py_builder_relayer_client.exceptions import RelayerClientException
    from py_builder_relayer_client.models import OperationType
except Exception as e:
    logging.exception("Failed to import Builder relayer SDK: %s", e)
    RelayClient = None
    BuilderConfig = None
    BuilderApiKeyCreds = None
    RelayerClientException = Exception
    OperationType = None


# Simple relay tx type: client.execute() in some SDK versions expects objects
# with .to, .data, .value, and sometimes .operation.
class _RelayTx:
    __slots__ = ("to", "data", "value", "operation")

    def __init__(self, to: str, data: str, value: str, operation=None):
        self.to = to
        self.data = data
        self.value = value
        # Default to a normal CALL operation when the enum is available
        if operation is not None:
            self.operation = operation
        elif OperationType is not None:
            self.operation = OperationType.Call
        else:
            self.operation = None

# Ensure we load env vars so we can get the DFLOW_API_KEY
load_dotenv()
DFLOW_API_KEY = os.getenv("DFLOW_API_KEY", "")

# Shared DB manager (SQLite + WAL)
db = DatabaseManager()

# Connect to public RPC nodes
# In production, these should be Infura/Alchemy or similar API keys.
polygon_rpc_url = os.getenv("POLYGON_RPC_URL", "https://rpc.ankr.com/polygon")
poly_w3 = Web3(Web3.HTTPProvider(polygon_rpc_url))

# Initialize read-only Polymarket CLOB client
clob_client = ClobClient("https://clob.polymarket.com")

# CoinGecko price cache (2 min TTL) - shared across all balance requests
_COINGECKO_CACHE: dict[str, tuple[float, dict]] = {}
_COINGECKO_LOCK = threading.Lock()
_COINGECKO_TTL = 120

# Short-lived raw markets cache to keep Telegram browsing snappy.
_RAW_MARKETS_CACHE: dict[tuple, tuple[float, list[dict], int]] = {}
_RAW_MARKETS_LOCK = threading.Lock()
_RAW_MARKETS_TTL = 12

def _get_coingecko_prices(ids_str: str) -> dict:
    now = time.monotonic()
    with _COINGECKO_LOCK:
        if ids_str in _COINGECKO_CACHE:
            expiry, data = _COINGECKO_CACHE[ids_str]
            if now < expiry:
                return data
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd",
            timeout=8,
        )
        data = resp.json() if resp.status_code == 200 else {}
        with _COINGECKO_LOCK:
            _COINGECKO_CACHE[ids_str] = (now + _COINGECKO_TTL, data)
        return data
    except Exception:
        return {}

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

def _compute_polygon_balances_rpc(address: str) -> dict | None:
    """
    Compute Polygon balances via direct RPC + CoinGecko.
    """
    try:
        checksum_address = Web3.to_checksum_address(address)

        # Pure-RPC approach: we cannot discover arbitrary ERC-20 contracts from the node,
        # so we query a curated list of important tokens plus any extra contracts configured
        # via environment variables.
        ERC20_ABI = json.loads(
            '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],'
            '"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],'
            '"type":"function"}]'
        )

        TOKENS = [
            # Circle USDC on Polygon (bridged)
            ("USDC",  "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6,  "usd-coin"),
            # Classic USDC.e on Polygon
            ("USDC.e", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6, "usd-coin"),
            ("USDT",  "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6,  "tether"),
            ("WETH",  "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", 18, "weth"),
            ("WBTC",  "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", 8,  "wrapped-bitcoin"),
            ("DAI",   "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", 18, "dai"),
            ("LINK",  "0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", 18, "chainlink"),
            ("AAVE",  "0xD6DF932A45C0f255f85145f286eA0b292B21C90B", 18, "aave"),
            ("UNI",   "0xb33EaAd8d922B1083446DC23f610c2567fB5180f", 18, "uniswap"),
            ("POL",   "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", 18, "polygon-ecosystem-token"),
        ]

        # Optional extra tokens via env: POLYGON_EXTRA_TOKENS as JSON array
        # [{"symbol":"USDe","address":"0x...","decimals":18,"coingecko_id":"ethena-usde"}, ...]
        extra_tokens_raw = os.getenv("POLYGON_EXTRA_TOKENS", "").strip()
        if extra_tokens_raw:
            try:
                extras = json.loads(extra_tokens_raw)
                for item in extras or []:
                    symbol = item.get("symbol")
                    address_hex = item.get("address")
                    decimals = int(item.get("decimals", 18))
                    cg_id = item.get("coingecko_id", "")
                    if symbol and address_hex:
                        TOKENS.append((symbol, address_hex, decimals, cg_id))
            except Exception as e:
                logging.error(f"Failed to parse POLYGON_EXTRA_TOKENS: {e}")

        # Build list of all symbols we want to report (including 0 balances)
        name_to_cg = {"POL (native)": "polygon-ecosystem-token"}
        for name, _, _, cg_id in TOKENS:
            name_to_cg[name] = cg_id

        coingecko_ids = sorted({cg_id for cg_id in name_to_cg.values() if cg_id})

        def _fetch_native():
            native_wei = poly_w3.eth.get_balance(checksum_address)
            return float(poly_w3.from_wei(native_wei, "ether"))

        def _fetch_coingecko():
            if not coingecko_ids:
                return {}
            ids_str = ",".join(coingecko_ids)
            return _get_coingecko_prices(ids_str)

        def _fetch_balance(item):
            name, contract_addr, decimals, _cg_id = item
            try:
                contract = poly_w3.eth.contract(
                    address=Web3.to_checksum_address(contract_addr),
                    abi=ERC20_ABI,
                )
                raw_balance = contract.functions.balanceOf(checksum_address).call()
                return (name, raw_balance / (10**decimals))
            except Exception:
                return (name, 0.0)

        # Run native balance, CoinGecko, and all ERC-20 fetches in parallel
        with ThreadPoolExecutor(max_workers=min(14, 2 + len(TOKENS))) as ex:
            native_future = ex.submit(_fetch_native)
            prices_future = ex.submit(_fetch_coingecko)
            balances = dict(ex.map(_fetch_balance, TOKENS))
            native_bal = native_future.result()
            prices = prices_future.result()

        tokens_out: list[dict] = []
        total_usd = 0.0

        # 1) Native POL (only if non-zero)
        pol_price = prices.get("polygon-ecosystem-token", {}).get("usd", 0)
        pol_usd = native_bal * pol_price
        if native_bal > 0:
            total_usd += pol_usd
            tokens_out.append(
                {
                    "symbol": "POL (native)",
                    "balance": float(native_bal),
                    "usd_value": float(pol_usd),
                }
            )

        # 2) ERC-20s from TOKENS
        for name, contract_addr, decimals, _cg_id in TOKENS:
            token_bal = balances.get(name, 0.0)
            cg_id = name_to_cg.get(name, "")
            usd_price = prices.get(cg_id, {}).get("usd", 0)
            token_usd = token_bal * usd_price
            if token_bal > 0:
                total_usd += token_usd
                tokens_out.append(
                    {
                        "symbol": name,
                        "balance": float(token_bal),
                        "usd_value": float(token_usd),
                    }
                )

        return {
            "wallet": checksum_address,
            "tokens": tokens_out,
            "total_usd": float(total_usd),
        }
    except Exception as e:
        logging.error(f"Error fetching Polygon balance: {e}")
        return None


def _compute_polygon_balances(address: str) -> dict | None:
    """
    Compute Polygon balances via RPC + CoinGecko.
    """
    return _compute_polygon_balances_rpc(address)


@mcp.tool()
def get_polygon_balance(address: str) -> str:
    """
    Get the full portfolio balance of a Polygon wallet across all major
    verified tokens, returned as a human-readable string (for bot usage).
    """
    data = _compute_polygon_balances(address)
    if not data:
        return "Error fetching Polygon balance."

    lines = ["📊 **Polygon Wallet Portfolio**\n"]
    for token in data["tokens"]:
        lines.append(
            f"  • {token['symbol']}: {token['balance']:.4f} (${token['usd_value']:.2f})"
        )
    lines.append(f"\n💰 **Total: ${data['total_usd']:.2f} USD**")
    return "\n".join(lines)


def get_polygon_balance_json(address: str) -> dict:
    """
    JSON-friendly Polygon balance summary for APIs.

    Returns:
      {
        "wallet": "0x...",
        "tokens": [{ "symbol", "balance", "usd_value" }, ...],
        "total_usd": number
      }
    """
    data = _compute_polygon_balances(address)
    if not data:
        return {
            "wallet": address,
            "tokens": [],
            "total_usd": 0.0,
            "error": "Error fetching Polygon balance",
        }
    return data


def get_usdc_e_balance_on_polygon(address: str) -> float | None:
    """
    Return USDC.e (0x2791...) balance for an address on Polygon in human units.
    Returns None on RPC/contract error.
    """
    if not address or not address.strip().startswith("0x"):
        return None
    try:
        from web3 import Web3
        rpc = os.getenv("POLYGON_RPC_URL") or "https://polygon-rpc.com"
        w3 = Web3(Web3.HTTPProvider(rpc))
        checksum = Web3.to_checksum_address(address)
        usdc_addr = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        abi = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]')
        contract = w3.eth.contract(address=usdc_addr, abi=abi)
        raw = contract.functions.balanceOf(checksum).call()
        return float(raw) / 1e6
    except Exception as e:
        logging.debug("get_usdc_e_balance_on_polygon failed for %s: %s", address[:20], e)
        return None


# Polymarket contract addresses on Polygon (from official docs + deployment resources)
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_OPERATOR = "0x71523d0f655B41E805Cec45b17163f528B59B820"
NEG_RISK_FEE_MODULE = "0x78769D50Be1763ed1CA0D5E878D93f05aabff29e"
MAX_APPROVAL = 2**256 - 1

# All 6 contracts that need approval (USDC spenders + CTF operators)
POLYMARKET_APPROVAL_CONTRACTS = [
    CTF_ADDRESS,
    NEG_RISK_ADAPTER,
    EXCHANGE_ADDRESS,
    NEG_RISK_EXCHANGE,
    NEG_RISK_OPERATOR,
    NEG_RISK_FEE_MODULE,
]

ERC20_APPROVE_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]')
ERC20_TRANSFER_ABI = json.loads('[{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]')
ERC1155_APPROVAL_ABI = json.loads('[{"inputs":[{"internalType":"address","name":"operator","type":"address"},{"internalType":"bool","name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]')
CTF_REDEEM_ABI = json.loads('[{"name":"redeemPositions","type":"function","stateMutability":"nonpayable","inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"outputs":[]}]')
ZERO_COLLECTION_ID = "0x" + "0" * 64


def _get_builder_relay_client(private_key: str):
    """
    Construct a Polymarket Builder RelayClient for gasless transactions using
    the Builder API credentials from the environment.
    """
    if RelayClient is None or BuilderConfig is None or BuilderApiKeyCreds is None:
        logging.error(
            "Builder relayer client not installed. Please install "
            "py-builder-relayer-client and py-builder-signing-sdk."
        )
        return None

    # Read builder API key credentials from our POLY_BUILDER_* env vars
    poly_key = os.getenv("POLY_BUILDER_API_KEY")
    poly_secret = os.getenv("POLY_BUILDER_SECRET")
    poly_pass = os.getenv("POLY_BUILDER_PASSPHRASE")
    if not poly_key or not poly_secret or not poly_pass:
        logging.error("Missing POLY_BUILDER_* environment variables for gasless relay.")
        return None

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=poly_key,
            secret=poly_secret,
            passphrase=poly_pass,
        )
    )

    # Match Polymarket example: RelayClient(relayer_url, chain_id, pk, builder_config)
    # Use production Polygon mainnet (matches USDC_POLYGON, CTF_ADDRESS); override via env for staging.
    relayer_url = os.getenv("RELAYER_URL", "https://relayer-v2.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    return RelayClient(
        relayer_url,
        chain_id,
        private_key,
        builder_config,
    )


def get_safe_address_for_user(eoa_address: str) -> str:
    """
    Return the expected Safe / proxy wallet address for a given EOA address.
    Uses cached value from DB when available; otherwise computes via relayer
    and persists. This is the address users should fund for gasless trading.
    """
    db_user = db.get_user_by_address(eoa_address)
    if not db_user:
        return ""
    cached = (db_user.get("safe_address") or "").strip()
    if cached:
        return cached
    if RelayClient is None:
        return ""
    private_key = db_user.get("eth_private_key")
    if not private_key:
        return ""
    client = _get_builder_relay_client(private_key)
    if client is None:
        return ""
    try:
        safe_addr = client.get_expected_safe()
        if safe_addr:
            db.update_safe_address(eoa_address, safe_addr)
        return safe_addr
    except Exception as e:
        logging.error(f"Error computing Safe address for {eoa_address}: {e}")
        return ""


def _get_trading_wallet_address(address: str) -> str:
    """
    Resolve the Polymarket trading wallet for a given EOA address.

    If a Safe / proxy wallet has been computed and stored for this user, use it;
    otherwise fall back to the original EOA.
    """
    try:
        db_user = db.get_user_by_address(address)
    except Exception:
        db_user = None
    if not db_user:
        return address
    # Prefer cached Safe address when available; otherwise compute it.
    safe_address = (db_user.get("safe_address") or "").strip()
    if not safe_address:
        safe_address = get_safe_address_for_user(address)
    return safe_address or address


def get_trading_wallet_address(address: str) -> str:
    """
    Public helper for resolving the Polymarket trading wallet (Safe when available).
    Used by the HTTP API and bots so that balances, portfolios, and trades all
    consistently follow the same wallet.
    """
    return _get_trading_wallet_address(address)


# Polymarket Bridge API: https://docs.polymarket.com/trading/bridge/deposit
POLYMARKET_BRIDGE_DEPOSIT_URL = "https://bridge.polymarket.com/deposit"


def get_polymarket_bridge_deposit_addresses(wallet_address: str) -> dict | None:
    """
    Get deposit addresses for the Polymarket Bridge (cross-chain deposits).
    Matches the web flow: POST /deposit with wallet address; returns addresses
    per chain type (evm, svm, btc, tvm). Assets sent there are bridged and
    swapped to USDC.e on Polygon automatically.
    """
    wallet_address = (wallet_address or "").strip()
    if not wallet_address or not wallet_address.startswith("0x"):
        return None
    try:
        resp = requests.post(
            POLYMARKET_BRIDGE_DEPOSIT_URL,
            json={"address": wallet_address},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code // 100 != 2:
            logging.warning("Polymarket bridge deposit API returned %s", resp.status_code)
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        # API returns {"address": {"evm": "...", "svm": "...", ...}} — unwrap.
        return data.get("address") if "address" in data and isinstance(data["address"], dict) else data
    except Exception as e:
        logging.warning("Polymarket bridge deposit API request failed: %s", e)
        return None


def withdraw_safe_to_eoa(address: str, amount: str = "all") -> str:
    """
    Withdraw USDC from the Safe trading wallet back to the owner's EOA.

    address: owner EOA address (used to derive Safe + relayer client)
    amount: USD amount as string, or "all" to withdraw full Safe balance.
    """
    db_user = db.get_user_by_address(address)
    if not db_user:
        return "Could not find wallet for this address."

    private_key = db_user.get("eth_private_key")
    if not private_key:
        return "User has no private key stored."

    # Resolve Safe / trading wallet
    safe_address = get_safe_address_for_user(address)
    if not safe_address:
        return "No Safe trading wallet found for this user. Nothing to withdraw."

    client = _get_builder_relay_client(private_key)
    if client is None:
        return "Gasless relay is not configured on this server."

    # Use Polygon RPC to read Safe's USDC balance
    w3 = Web3(Web3.HTTPProvider(polygon_rpc_url))
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_POLYGON),
        abi=ERC20_TRANSFER_ABI
        + json.loads(
            '[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'
        ),
    )

    safe_addr = Web3.to_checksum_address(safe_address)
    owner_addr = Web3.to_checksum_address(address)

    try:
        balance_raw = usdc.functions.balanceOf(safe_addr).call()
    except Exception as e:
        logging.error(f"Failed to read Safe USDC balance: {e}")
        return "Error fetching Safe wallet balance."

    if balance_raw <= 0:
        return "Safe trading wallet has no USDC.e to withdraw."

    if amount == "all":
        transfer_amount = balance_raw
    else:
        try:
            amt_f = float(amount)
            if amt_f <= 0:
                return "Withdraw amount must be positive."
            # USDC has 6 decimals
            wanted = int(amt_f * 1e6)
        except (TypeError, ValueError):
            return f"Invalid withdraw amount '{amount}'."
        transfer_amount = min(balance_raw, wanted)

    if transfer_amount <= 0:
        return "Withdraw amount is zero after rounding."

    # Build Safe tx: USDC.transfer(owner, amount)
    try:
        tx_data = usdc.encode_abi(
            "transfer",
            args=[owner_addr, transfer_amount],
        )
    except Exception as e:
        logging.error(f"Failed to encode USDC transfer for withdraw: {e}")
        return "Error building withdraw transaction."

    tx = _RelayTx(
        to=USDC_POLYGON,
        data=tx_data,
        value="0",
    )

    try:
        _ensure_safe_deployed(client)
        resp = client.execute([tx], "Withdraw USDC.e from Safe to EOA")
        result = resp.wait()

        tx_hash = ""
        if isinstance(result, dict):
            tx_hash = (
                result.get("txHash")
                or result.get("transactionHash")
                or result.get("hash")
                or ""
            )

        return (
            "✅ Withdraw submitted from Safe trading wallet to your EOA.\n"
            f"Amount: {transfer_amount / 1e6:.4f} USDC.e\n"
            f"Safe: `{safe_address}`\n"
            f"EOA: `{address}`\n"
            f"Transaction: {tx_hash or '[pending]'}"
        )
    except Exception as e:
        logging.error(f"Safe → EOA withdraw via relayer failed: {e}")
        return f"❌ Withdraw via relayer failed: {str(e)}"


def transfer_usdc_to_safe(address: str, amount: str = "all") -> str:
    """
    Transfer bridged USDC (USDC_POLYGON) from the owner's EOA to the Safe trading wallet.

    This is the inverse of withdraw_safe_to_eoa: funds move from EOA → Safe.

    address: owner EOA address (used to derive Safe)
    amount: USD amount as string, or "all" to transfer the full EOA USDC balance.
    """
    db_user = db.get_user_by_address(address)
    if not db_user:
        return "Could not find wallet for this address."

    private_key = db_user.get("eth_private_key")
    if not private_key:
        return "User has no private key stored."

    # Resolve Safe / trading wallet
    safe_address = get_safe_address_for_user(address)
    if not safe_address:
        return "No Safe trading wallet found for this user. Nothing to transfer."

    # Use Polygon RPC to read EOA's USDC balance and send to Safe
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    from web3.middleware import ExtraDataToPOAMiddleware

    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_POLYGON),
        abi=ERC20_TRANSFER_ABI
        + json.loads(
            '[{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'
        ),
    )

    acct = w3.eth.account.from_key(private_key)
    eoa_addr = acct.address
    safe_addr = Web3.to_checksum_address(safe_address)

    try:
        balance_raw = usdc.functions.balanceOf(eoa_addr).call()
    except Exception as e:
        logging.error(f"Failed to read EOA USDC balance for transfer_to_safe: {e}")
        return "Error fetching EOA USDC.e balance."

    if balance_raw <= 0:
        return "EOA wallet has no USDC.e to transfer to Safe."

    if amount == "all":
        transfer_amount = balance_raw
    else:
        try:
            amt_f = float(amount)
            if amt_f <= 0:
                return "Transfer amount must be positive."
            # USDC has 6 decimals
            wanted = int(amt_f * 1e6)
        except (TypeError, ValueError):
            return f"Invalid transfer amount '{amount}'."
        transfer_amount = min(wanted, balance_raw)

    try:
        nonce = w3.eth.get_transaction_count(eoa_addr)
        tx = usdc.functions.transfer(safe_addr, transfer_amount).build_transaction(
            {
                "from": eoa_addr,
                "nonce": nonce,
                "gas": 120000,
                "gasPrice": w3.eth.gas_price,
                "chainId": 137,
            }
        )
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logging.info(f"[TransferToSafe] USDC.e transfer TX: {tx_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    except Exception as e:
        logging.error(f"Failed to transfer USDC.e from EOA to Safe: {e}")
        return "Error sending transfer transaction to Safe."

    return "Successfully transferred USDC.e from EOA to Safe."


def _ensure_safe_deployed(client) -> None:
    """
    Ensure the per-user Safe/proxy is deployed before first gasless tx.
    This is idempotent: if the Safe already exists, the relayer will no-op.
    """
    try:
        deploy_resp = client.deploy()
        deploy_result = deploy_resp.wait()
        logging.info("Safe deploy result: %s", deploy_result)
    except Exception as e:
        # If the Safe is already deployed or deploy is not required, relayer
        # may throw; we log and continue so gasless tx can still be attempted.
        logging.info("Safe deploy may already exist or failed softly: %s", e)


def _fetch_closed_positions(address: str) -> list[dict]:
    """Fetch closed/resolved Polymarket positions for an address via Data API."""
    # Always query using the trading wallet (Safe when available).
    trading_addr = _get_trading_wallet_address(address)
    try:
        url = f"https://data-api.polymarket.com/closed-positions?user={trading_addr}&limit=50"
        resp = requests.get(url, timeout=10)
        data = resp.json() if resp.status_code == 200 else []
        return data if isinstance(data, list) else []
    except Exception as e:
        logging.error(f"Error fetching closed positions for {address}: {e}")
        return []


def _fetch_payout_info(condition_id: str) -> dict | None:
    """Fetch payout information for a resolved condition from the Data API."""
    try:
        # Use the conditions endpoint to get payout info
        url = f"https://data-api.polymarket.com/v1/conditions?conditionIds={condition_id}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
        return None
    except Exception as e:
        logging.error(f"Error fetching payout info for {condition_id}: {e}")
        return None


def _get_winning_indices(payout_info: dict) -> list[int]:
    """
    Extract winning outcome indices from payout info.
    Returns 1-indexed indices as required by redeemPositions.

    Payout vector example:
    - [1, 0] means outcome 0 (index 1) won
    - [0, 1] means outcome 1 (index 2) won
    """
    payout = payout_info.get("payouts") or payout_info.get("payout") or []
    if not payout or not isinstance(payout, list):
        return []

    winning = []
    for i, amount in enumerate(payout):
        if amount > 0:
            # Convert to 1-indexed as required by CTF
            winning.append(i + 1)

    return winning


@mcp.tool()
def claim_polymarket_winnings(address: str) -> str:
    """
    Attempt to redeem winnings from resolved Polymarket markets using the
    gasless Builder relayer.

    Flow:
    1. Fetch closed positions for the user's trading wallet
    2. For each unique conditionId, fetch payout info to determine winner
    3. Only redeem the winning outcome index (not both)
    4. Submit via gasless relay to Safe wallet
    """
    db_user = db.get_user_by_address(address)
    if not db_user:
        return "Could not find wallet for this address."

    private_key = db_user["eth_private_key"]
    client = _get_builder_relay_client(private_key)
    if client is None:
        return "Gasless relay is not configured on this server."

    closed = _fetch_closed_positions(address)
    if not closed:
        return "No unclaimed winnings."

    # Group positions by conditionId and track which ones have winnings
    condition_payouts: dict[str, dict] = {}
    for p in closed:
        cid = p.get("conditionId") or p.get("condition_id") or ""
        if cid:
            condition_payouts[cid] = p

    if not condition_payouts:
        return "No unclaimed winnings."

    w3 = Web3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_REDEEM_ABI)

    txs: list[_RelayTx] = []
    claimed_count = 0

    for cid, pos_info in condition_payouts.items():
        try:
            # Fetch payout info to determine winning outcome
            payout_info = _fetch_payout_info(cid)
            if not payout_info:
                logging.warning(f"Could not fetch payout info for condition {cid}")
                continue

            # Get winning indices (1-indexed)
            winning_indices = _get_winning_indices(payout_info)
            if not winning_indices:
                logging.warning(f"No winning outcomes found for condition {cid}")
                continue

            # Ensure bytes32 formatting
            condition_bytes = bytes.fromhex(cid[2:]) if cid.startswith("0x") else bytes.fromhex(cid)
            if len(condition_bytes) != 32:
                logging.warning(f"Skipping conditionId with invalid length: {cid}")
                continue

            # Build redeem call with only winning outcomes
            redeem_data = ctf.encode_abi(
                "redeemPositions",
                args=[USDC_POLYGON, ZERO_COLLECTION_ID, cid, winning_indices],
            )
            txs.append(
                _RelayTx(
                    to=CTF_ADDRESS,
                    data=redeem_data,
                    value="0",
                )
            )
            claimed_count += 1
            logging.info(f"Queued redemption for condition {cid[:16]}... winning indices: {winning_indices}")

        except Exception as e:
            logging.error(f"Failed to build redeem tx for condition {cid}: {e}")
            continue

    if not txs:
        return "No unclaimed winnings."

    try:
        # Auto-deploy Safe once for this user (idempotent)
        _ensure_safe_deployed(client)
        resp = client.execute(txs, "Redeem Polymarket positions")
        result = resp.wait()

        if not isinstance(result, dict):
            tx_hash = getattr(resp, "transaction_hash", "") or getattr(
                resp, "tx_hash", ""
            )
            logging.error(
                "Gasless redeem via relayer returned non-dict result: %r (tx=%s)",
                result,
                tx_hash,
            )
            return "No unclaimed winnings."

        tx_hash = result.get("txHash") or result.get("transactionHash") or ""
        status = (result.get("status") or result.get("txStatus") or "").lower()
        if status and status not in ("success", "succeeded", "ok", "confirmed"):
            logging.error("Gasless redeem tx reported failure status: %s", status)
            return (
                "❌ Claim via relayer failed on-chain.\n"
                f"Status: {status}\n"
                f"Transaction: {tx_hash or '[unknown]'}"
            )

        return (
            f"✅ Submitted gasless claim for {claimed_count} position(s) via Polymarket relayer.\n"
            f"Transaction: {tx_hash or '[pending]'}"
        )
    except Exception as e:
        logging.error(f"Gasless redeem via relayer failed: {e}")
        low = str(e).lower()
        if "did not return a receipt" in low or "receipt" in low and "none" in low:
            return "No unclaimed winnings."
        return f"❌ Claim via relayer failed: {str(e)}"


def _run_gasless_approve(user_id: int, address: str) -> bool:
    """Reset flag, run full gasless approval (Safe deploy + allowances), update flag. Returns success."""
    try:
        with db.transaction() as conn:
            conn.execute("UPDATE users SET polymarket_approved = 0 WHERE user_id = ?;", (user_id,))
        result = approve_usdc_for_trading(address)
        ok = not str(result).lstrip().startswith("❌")
        if ok:
            with db.transaction() as conn:
                conn.execute("UPDATE users SET polymarket_approved = 1 WHERE user_id = ?;", (user_id,))
            logging.info("Gasless re-approval succeeded for user %s", user_id)
        else:
            logging.warning("Gasless re-approval returned failure for user %s: %s", user_id, result)
        return ok
    except Exception as e:
        logging.warning("Gasless re-approval failed for user %s: %s", user_id, e)
        return False


def _log_clob_error(
    e: Exception,
    context: str,
    address: str | None = None,
    request_ctx: dict | None = None,
) -> None:
    """Log CLOB API error with status code, response body, and request context for debugging 400s."""
    status_code = getattr(e, "status_code", None)
    err_body = getattr(e, "error_message", None)
    try:
        body_str = json.dumps(err_body, default=str) if err_body is not None else repr(err_body)
    except Exception:
        body_str = repr(err_body)
    logging.error(
        "CLOB %s: HTTP %s | address=%s | response=%s",
        context,
        status_code if status_code is not None else "?",
        address or "?",
        body_str,
    )
    if request_ctx:
        try:
            ctx_str = json.dumps(request_ctx, default=str)
        except Exception:
            ctx_str = repr(request_ctx)
        logging.error("CLOB 400 DEBUG request_ctx=%s", ctx_str)
    if status_code == 400 and err_body:
        err_msg = err_body.get("error", str(err_body)) if isinstance(err_body, dict) else str(err_body)
        logging.error(
            "CLOB HTTP 400: %s | Check: balance/allowance for trading wallet, funder=Safe for sells, token_id/amount valid.",
            err_msg,
        )


@mcp.tool()
def approve_usdc_for_trading(address: str) -> str:
    """
    Set all USDC + CTF allowances for Polymarket trading using the gasless
    Builder relayer. Approves all 6 Polymarket contracts (USDC spend + CTF operator).
    Must be called once before the first trade.
    """
    db_user = db.get_user_by_address(address)
    if not db_user:
        return "Could not find wallet for this address."

    private_key = db_user["eth_private_key"]
    client = _get_builder_relay_client(private_key)
    if client is None:
        return "Gasless relay is not configured on this server."

    # We only need Web3 for ABI encoding; no on-chain send from here.
    w3 = Web3()
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_POLYGON), abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_APPROVAL_ABI)

    # 1) USDC.approve(spender, MAX_UINT256) for all 6 contracts that pull collateral.
    # 2) CTF.setApprovalForAll(operator, true) for all 6 so they can move ERC-1155 positions.
    txs: list[_RelayTx] = []

    for spender in POLYMARKET_APPROVAL_CONTRACTS:
        spender_addr = Web3.to_checksum_address(spender)
        approve_data = usdc.encode_abi("approve", args=[spender_addr, MAX_APPROVAL])
        txs.append(
            _RelayTx(
                to=USDC_POLYGON,
                data=approve_data,
                value="0",
            )
        )

    for operator in POLYMARKET_APPROVAL_CONTRACTS:
        operator_addr = Web3.to_checksum_address(operator)
        set_approval_data = ctf.encode_abi("setApprovalForAll", args=[operator_addr, True])
        txs.append(
            _RelayTx(
                to=CTF_ADDRESS,
                data=set_approval_data,
                value="0",
            )
        )

    try:
        # Auto-deploy Safe once for this user (idempotent)
        _ensure_safe_deployed(client)
        resp = client.execute(txs, "Polymarket USDC/CTF approvals")
        result = resp.wait()

        if not isinstance(result, dict):
            tx_hash = getattr(resp, "transaction_hash", "") or getattr(
                resp, "tx_hash", ""
            )
            logging.error(
                "Gasless approvals via relayer returned non-dict result: %r (tx=%s)",
                result,
                tx_hash,
            )
            return (
                "❌ Approval via relayer failed: transaction did not return a receipt.\n"
                f"Transaction: {tx_hash or '[unknown]'}"
            )

        tx_hash = result.get("txHash") or result.get("transactionHash") or ""
        status = (result.get("status") or result.get("txStatus") or "").lower()
        if status and status not in ("success", "succeeded", "ok", "confirmed"):
            logging.error("Gasless approval tx reported failure status: %s", status)
            return (
                "❌ Approval via relayer failed on-chain.\n"
                f"Status: {status}\n"
                f"Transaction: {tx_hash or '[unknown]'}"
            )

        return (
            "✅ Submitted gasless approval transactions via Polymarket relayer.\n"
            f"Transaction: {tx_hash or '[pending]'}"
        )
    except Exception as e:
        logging.error(f"Gasless approvals via relayer failed: {e}")
        return f"❌ Approval via relayer failed: {str(e)}"


def _fetch_positions(address: str) -> list:
    """Fetch open positions from Polymarket Data API."""
    trading_addr = _get_trading_wallet_address(address)
    try:
        url = f"https://data-api.polymarket.com/positions?user={trading_addr}"
        resp = requests.get(url, timeout=10)
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        logging.error(f"Error fetching portfolio positions: {e}")
        return []


def get_polymarket_open_pnl(address: str) -> float:
    """
    Compute total open (unrealised) PnL across all current Polymarket positions
    for a given wallet, using the Data API.
    """
    positions = _fetch_positions(address) or []
    total_pnl = 0.0
    for p in positions:
        try:
            total_pnl += float(p.get("cashPnl", 0) or 0)
        except (TypeError, ValueError):
            continue
    return float(total_pnl)


@mcp.tool()
def get_polymarket_portfolio(address: str) -> str:
    """Get a complete overview of the user's Polymarket portfolio, including funds and open positions."""
    # Fetch balance and positions in parallel
    trading_addr = _get_trading_wallet_address(address)
    with ThreadPoolExecutor(max_workers=2) as ex:
        balance_future = ex.submit(get_polygon_balance, trading_addr)
        positions_future = ex.submit(_fetch_positions, trading_addr)
        on_chain_summary = balance_future.result()
        positions = positions_future.result()
    
    lines = [
        "📊 **POLYMARKET PORTFOLIO OVERVIEW**",
        f"Wallet: `{trading_addr}`",
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


def get_polymarket_portfolio_with_positions(address: str) -> tuple[str, list[dict]]:
    """
    Return (portfolio_text, positions) for bot use.
    positions: list of {condition_id, outcome, title, size, currentValue, ...}
    """
    trading_addr = _get_trading_wallet_address(address)
    with ThreadPoolExecutor(max_workers=2) as ex:
        balance_future = ex.submit(get_polygon_balance, trading_addr)
        positions_future = ex.submit(_fetch_positions, trading_addr)
        on_chain_summary = balance_future.result()
        positions = positions_future.result() or []

    lines = [
        "📊 **POLYMARKET PORTFOLIO OVERVIEW**",
        f"Wallet: `{trading_addr}`",
        "\n💰 **On-Chain Funds**",
        on_chain_summary.split("\n", 2)[-1] if "\n" in on_chain_summary else on_chain_summary,
    ]

    pos_list: list[dict] = []
    if not positions:
        lines.append("\n📈 **Open Positions**: None")
    else:
        lines.append(f"\n📈 **Open Positions ({len(positions)})**")
        total_pnl = 0.0
        portfolio_value = 0.0
        for p in positions:
            title = p.get("title", "Unknown Market")
            api_outcome = p.get("outcome", "Unknown")
            condition_id = p.get("conditionId") or p.get("condition_id") or ""
            asset = p.get("asset") or p.get("token_id") or ""
            outcome_index = p.get("outcomeIndex")
            size = float(p.get("size", 0))
            avg_price = float(p.get("avgPrice", 0))
            cur_price = float(p.get("curPrice", 0))
            cur_val = float(p.get("currentValue", 0))
            pnl_pct = float(p.get("percentPnl", 0))
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            # Resolve to market's actual outcome name (e.g. NSH not Yes/Under) so sell uses correct token
            outcome = api_outcome
            if condition_id:
                m = market_cache.ensure_market_cached(condition_id)
                if m and m.outcomes:
                    if asset:
                        try:
                            idx = m.clob_token_ids.index(str(asset).strip())
                            if 0 <= idx < len(m.outcomes):
                                outcome = m.outcomes[idx]
                        except (ValueError, AttributeError):
                            pass
                    if outcome == api_outcome and outcome_index is not None:
                        try:
                            i = int(outcome_index)
                            if 0 <= i < len(m.outcomes):
                                outcome = m.outcomes[i]
                        except (TypeError, ValueError):
                            pass
                    if outcome == api_outcome and len(m.outcomes) >= 2:
                        low = api_outcome.strip().lower()
                        if low in ("yes", "1", "long"):
                            outcome = m.outcomes[0]
                        elif low in ("no", "0", "short"):
                            outcome = m.outcomes[1]
                    if outcome == api_outcome:
                        matched = next((o for o in m.outcomes if o.lower() == (api_outcome or "").lower()), None)
                        if matched:
                            outcome = matched
            lines.append(
                f"• **{title}**\n"
                f"  Side: {outcome} | Size: {size:.2f} shares\n"
                f"  Buy: {avg_price*100:.1f}¢ | Now: {cur_price*100:.1f}¢ | Value: ${cur_val:.2f}\n"
                f"  PnL: {pnl_emoji} {pnl_pct:+.2f}%"
            )
            total_pnl += float(p.get("cashPnl", 0))
            portfolio_value += cur_val
            pos_list.append({
                "condition_id": condition_id,
                "outcome": outcome,
                "title": title,
                "size": size,
                "cur_price": cur_price,
                "current_value": cur_val,
            })
        lines.append(f"\n💵 **Total Portfolio Value: ${portfolio_value:.2f}**")
        lines.append(f"{'🟢' if total_pnl >= 0 else '🔴'} **Total PnL: ${total_pnl:+.2f}**")

    return "\n".join(lines), pos_list


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
    from web3 import Web3
    import time
    
    db_user = db.get_user_by_address(address)
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
            f"Approvals are auto-run before your first trade."
        )
        
    except Exception as e:
        logging.error(f"USDC swap failed: {e}")
        return f"❌ Swap failed: {str(e)}"

def _fetch_and_cache_events(url_params: dict, query_filter: str = None, header: str = "Active Markets") -> str:
    """Shared logic: fetch events from Gamma, populate market_cache, return formatted text."""
    from datetime import datetime, timezone
    
    url = "https://gamma-api.polymarket.com/events"
    response = requests.get(url, params=url_params)
    
    if response.status_code != 200:
        return "Error fetching markets from Polymarket API."
    
    data = response.json()
    # Gamma /events returns a list directly; /events/pagination returns {"data": [...]}
    events = data if isinstance(data, list) else data.get("data", [])
    if not events:
        return "No active markets found on Polymarket right now."
    
    now = datetime.now(timezone.utc)
    market_cache.clear()
    
    for event in events:
        # Skip closed or inactive events
        if event.get("closed") or not event.get("active", True):
            continue
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
        # Try to capture a representative image for this event/market
        event_image = (
            event.get("image")
            or event.get("coverImageUrl")
            or event.get("cover_image_url")
        )
        
        if query_filter and query_filter.lower() not in title.lower():
            continue

        event_slug = (event.get("slug") or "").strip()
        
        for market in event.get("markets", [])[:5]:
            # Skip closed/inactive markets - only show active (open) markets
            if (
                market.get("closed")
                or not market.get("active", True)
                or not market.get("acceptingOrders", True)
            ):
                continue
            # Get 24h volume (don't skip if zero - some good markets might have low volume)
            try:
                vol_24h = float(market.get("volume24hr", 0) or 0)
            except Exception:
                vol_24h = 0.0
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
            
            image_url = (
                market.get("image")
                or market.get("coverImageUrl")
                or market.get("cover_image_url")
                or event_image
            )

            # Best-effort deep link to Polymarket UI using event + market slugs.
            market_slug = (market.get("slug") or "").strip()
            market_url = None
            if event_slug and market_slug:
                market_url = f"https://polymarket.com/event/{event_slug}/{market_slug}"

            try:
                liquidity = float(market.get("liquidity", 0) or 0)
            except Exception:
                liquidity = 0.0

            market_cache.add(
                question=m_question,
                event_title=title,
                condition_id=condition_id,
                outcomes=list(outcomes),
                clob_token_ids=list(tokens_raw),
                odds=odds,
                end_date=end_date_str,
                image_url=image_url,
                volume_24h=vol_24h,
                liquidity=liquidity,
                url=market_url,
            )
        
        # Stop after caching 20 markets to keep output manageable
        if len(market_cache.list_all()) >= 20:
            break

    # If we didn't manage to cache any markets, give a clearer, mode-specific message
    if not market_cache.list_all():
        if header == "Markets closing soon":
            return "No active markets are currently closing soon. Try *Trending* or *By 24h volume* instead."
        if header.startswith("Markets in '"):
            return "No active markets found for this category right now. Try *Trending* or another category."
        return "No active markets matched this filter. Try *Trending* markets."

    return market_cache.format_all()


def fetch_polymarket_markets_raw(
    *,
    order: str = "volume24hr",
    ascending: bool = False,
    tag_slug: str | None = None,
    page: int = 1,
    page_size: int = 5,
) -> tuple[list[dict], int]:
    """
    Fetch active markets directly from Gamma API without touching market_cache.
    Returns (markets, total_pages_guess).
    """
    cache_key = (order, bool(ascending), (tag_slug or "").strip().lower(), int(page), int(page_size))
    now_mono = time.monotonic()
    with _RAW_MARKETS_LOCK:
        cached = _RAW_MARKETS_CACHE.get(cache_key)
        if cached and now_mono < cached[0]:
            return cached[1], cached[2]

    params = {
        "active": "true",
        "closed": "false",
        "order": order,
        "ascending": "true" if ascending else "false",
        "limit": str(max(40, page_size * 8)),
        "offset": str(max(0, (page - 1) * page_size)),
    }
    if tag_slug:
        params["tag_slug"] = tag_slug

    try:
        resp = requests.get("https://gamma-api.polymarket.com/events", params=params, timeout=10)
        if resp.status_code != 200:
            return [], 1
        data = resp.json()
        events = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(events, list):
            return [], 1

        out: list[dict] = []
        for event in events:
            if event.get("closed") or not event.get("active", True):
                continue
            event_title = event.get("title") or "No Title"
            event_slug = (event.get("slug") or "").strip()
            event_image = event.get("image") or event.get("coverImageUrl") or event.get("cover_image_url")
            for market in event.get("markets", [])[:8]:
                if (
                    market.get("closed")
                    or not market.get("active", True)
                    or not market.get("acceptingOrders", True)
                ):
                    continue
                try:
                    vol_24h = float(market.get("volume24hr", 0) or 0)
                except Exception:
                    vol_24h = 0.0
                if vol_24h <= 0:
                    continue

                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                token_ids = market.get("clobTokenIds", [])
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)
                if not outcomes or not token_ids:
                    continue

                # Use Gamma-provided outcome prices first (much faster than per-token midpoint calls).
                odds: dict[str, int] = {}
                prices_raw = market.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    try:
                        prices_raw = json.loads(prices_raw)
                    except Exception:
                        prices_raw = []
                if isinstance(prices_raw, list) and prices_raw:
                    for o_name, p in zip(outcomes, prices_raw):
                        try:
                            odds[str(o_name)] = int(float(p) * 100)
                        except Exception:
                            odds[str(o_name)] = 0
                else:
                    # Fallback: no prices present in payload.
                    for o_name in outcomes:
                        odds[str(o_name)] = 0

                market_slug = (market.get("slug") or "").strip()
                url = (
                    f"https://polymarket.com/event/{event_slug}/{market_slug}"
                    if event_slug and market_slug
                    else "https://polymarket.com"
                )
                image_url = (
                    market.get("image")
                    or market.get("coverImageUrl")
                    or market.get("cover_image_url")
                    or event_image
                )
                try:
                    liquidity = float(market.get("liquidity", 0) or 0)
                except Exception:
                    liquidity = 0.0

                out.append(
                    {
                        "condition_id": (market.get("conditionId") or "").strip(),
                        "question": market.get("question") or "",
                        "event_title": event_title,
                        "outcomes": list(outcomes),
                        "clob_token_ids": list(token_ids),
                        "odds": odds,
                        "end_date": market.get("endDate") or event.get("endDate") or "",
                        "volume_24h": vol_24h,
                        "liquidity": liquidity,
                        "image_url": image_url,
                        "url": url,
                    }
                )

        # We already request page offset from Gamma, so avoid re-slicing by page offset.
        subset = out[:page_size]
        total_pages_guess = max(1, (len(out) + page_size - 1) // page_size)
        with _RAW_MARKETS_LOCK:
            _RAW_MARKETS_CACHE[cache_key] = (time.monotonic() + _RAW_MARKETS_TTL, subset, total_pages_guess)
        return subset, total_pages_guess
    except Exception as e:
        logging.warning("fetch_polymarket_markets_raw failed: %s", e)
        return [], 1


@mcp.tool()
def get_polymarket_markets() -> str:
    """Fetch trending open prediction markets from Polymarket. Each market gets a #ID you can use for trading."""
    try:
        return _fetch_and_cache_events(
            url_params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": 100,
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
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": 100,
            },
            query_filter=query,
            header=f"Markets matching '{query}'"
        )
    except Exception as e:
        logging.error(f"Error searching Polymarket markets: {e}")
        return "Error searching Polymarket markets."


@mcp.tool()
def get_polymarket_markets_by_category(category: str) -> str:
    """Fetch Polymarket markets filtered by category (tag slug). Each market gets a #ID you can use for trading."""
    try:
        return _fetch_and_cache_events(
            url_params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": 100,
                "tag_slug": category,
            },
            header=f"Markets in '{category}'"
        )
    except Exception as e:
        logging.error(f"Error fetching Polymarket category markets: {e}")
        return "Error fetching category markets."


@mcp.tool()
def get_polymarket_markets_by_tag(tag_id: int, include_related: bool = False) -> str:
    """
    Fetch Polymarket markets filtered by a numeric tag_id (Gamma tags / sports).

    This wraps the Gamma /events endpoint:

        GET https://gamma-api.polymarket.com/events?tag_id=...&related_tags=...

    and then uses the shared _fetch_and_cache_events helper to populate
    market_cache and return a formatted list of active markets.
    """
    try:
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": 100,
            "tag_id": str(tag_id),
        }
        if include_related:
            params["related_tags"] = "true"
        return _fetch_and_cache_events(
            url_params=params,
            header=f"Markets for tag_id {tag_id}",
        )
    except Exception as e:
        logging.error(f"Error fetching Polymarket tag markets: {e}")
        return "Error fetching tag-based markets."


@mcp.tool()
def get_polymarket_markets_closing_soon() -> str:
    """
    Fetch Polymarket markets that are closing soon (sorted by endDate ascending).
    Uses Gamma /events with order=endDate and ascending=true, then populates the
    shared market_cache.
    """
    try:
        return _fetch_and_cache_events(
            url_params={
                "active": "true",
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
                "limit": 100,
            },
            header="Markets closing soon",
        )
    except Exception as e:
        logging.error(f"Error fetching closing-soon markets: {e}")
        return "Error fetching markets closing soon."


@mcp.tool()
def get_market_by_id(market_id: int) -> str:
    """
    Look up details of a cached market by its #ID number.

    Returns a user-friendly summary suitable for Telegram:
      - Question
      - Event title
      - Human-readable end date
      - Odds as percentages (Yes/No), not raw cents

    Internal fields like condition_id and token IDs are kept in the cache for
    trading, but are not shown to the user.
    """
    from datetime import datetime

    m = market_cache.get(market_id)
    if not m:
        return "Market not found. Please refresh the markets list."

    # Format odds as percentages plus underlying cents
    odds_lines = []
    for outcome in m.outcomes:
        cents = m.odds.get(outcome, "?")
        if cents == "?":
            odds_lines.append(f"- {outcome}: ?")
            continue
        try:
            pct = float(cents)
            odds_lines.append(f"- {outcome}: {pct:.0f}%  ({pct:.1f}¢)")
        except Exception:
            odds_lines.append(f"- {outcome}: {cents}¢")
    odds_block = "\n".join(odds_lines) if odds_lines else "No live odds available."

    # Friendly end date
    end_str = m.end_date or ""
    pretty_end = "TBD"
    if end_str:
        try:
            iso = end_str
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            pretty_end = dt.strftime("%b %d, %Y %H:%M UTC")
        except Exception:
            pretty_end = end_str

    # Market stats
    stats_lines = []
    if m.volume_24h:
        stats_lines.append(f"- Volume (24h): ${m.volume_24h:,.2f}")
    if m.liquidity:
        stats_lines.append(f"- Liquidity: ${m.liquidity:,.2f}")
    stats_block = "\n".join(stats_lines) if stats_lines else "No recent volume data."

    return (
        f"🧠 {m.question}\n"
        f"{m.event_title}\n\n"
        f"📊 *Current prices*\n"
        f"{odds_block}\n\n"
        f"📈 *Market stats*\n"
        f"{stats_block}\n\n"
        f"🕒 *Timeline*\n"
        f"- Expires: {pretty_end}"
    )


@mcp.tool()
def execute_trade(market_id: int, side: str, amount: str, address: str) -> str:
    """Execute a REAL trade on Polymarket. Use the #ID from the market list. side must be 'Yes' or 'No'. amount is in USD."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.exceptions import PolyApiException

    m = market_cache.get(market_id)
    if not m:
        return f"Market #{market_id} not found in cache. Use get_polymarket_markets or search_polymarket_events first."

    side = side.strip().capitalize()
    if side not in m.outcomes:
        return f"Invalid side '{side}'. Available outcomes: {', '.join(m.outcomes)}"

    try:
        amount_float = float(amount)
    except (ValueError, TypeError):
        return f"Invalid amount '{amount}'. Use a number in USD (e.g. 10 or 5.50)."
    if amount_float < 1.0:
        return f"Amount ${amount_float:.2f} is below Polymarket minimum ($1). Use at least $1."

    side_idx = m.outcomes.index(side)
    token_id = m.clob_token_ids[side_idx]
    odds_cents = m.odds.get(side, 0)

    db_user = db.get_user_by_address(address)
    if not db_user:
        return "CRITICAL: Could not find user private key for this wallet address."

    user_id = db_user["user_id"]
    private_key = db_user["eth_private_key"]
    owner_addr = db_user.get("eth_address") or ""
    trading_addr = get_trading_wallet_address(owner_addr)
    use_safe = trading_addr and trading_addr.lower() != owner_addr.lower()

    # Auto-approve if not yet approved (includes Safe deploy)
    if not (db_user.get("polymarket_approved") or 0):
        _run_gasless_approve(user_id, address)

    for attempt in range(2):
        try:
            # Per Polymarket skill: one client with funder, set_api_creds(create_or_derive_api_creds())
            client = ClobClient(
                host="https://clob.polymarket.com", chain_id=137, key=private_key,
                signature_type=2 if use_safe else 0,
                funder=trading_addr if use_safe else None,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            order_args = MarketOrderArgs(token_id=token_id, amount=amount_float, side="BUY")
            signed_order = client.create_market_order(order_args)
            resp = client.post_order(signed_order, orderType=OrderType.FOK)

            if isinstance(resp, dict):
                order_id = (
                    resp.get("orderID") or resp.get("orderId") or resp.get("order_id")
                    or resp.get("id") or resp.get("hash") or "N/A"
                )
                order_id = str(order_id).strip() if order_id else "N/A"
                status = resp.get("status") or resp.get("orderStatus") or "submitted"
                tx_hashes = resp.get("transactionsHashes") or resp.get("transactionHashes") or []
                tx_hash = (
                    (tx_hashes[0] if tx_hashes else None)
                    or resp.get("transactHash") or resp.get("txHash") or resp.get("transactionHash") or "pending"
                )
                tx_hash = str(tx_hash) if tx_hash else "pending"
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

        except PolyApiException as e:
            _log_clob_error(
                e,
                "post_order (market)",
                address=address,
                request_ctx={
                    "token_id": token_id,
                    "amount": amount_float,
                    "side": side,
                    "order_side": "BUY",
                    "trading_addr": trading_addr,
                    "use_safe": use_safe,
                    "market_id": market_id,
                },
            )
            err = getattr(e, "error_message", {}) or {}
            msg = (err.get("error") or str(e)).lower()
            if attempt == 0 and ("allowance" in msg or "not enough balance" in msg):
                logging.warning("Trade failed with allowance error; forcing re-approval for %s", address)
                _run_gasless_approve(user_id, address)
                continue
            logging.error("Trade execution failed: %s", msg)
            return (
                "❌ TRADE FAILED: not enough balance or allowance.\n"
                "Make sure you have USDC.e in your Safe wallet (see /balance)."
            )

        except Exception as e:
            logging.error("Trade execution failed: %s", e)
            return f"❌ TRADE FAILED: {e}"

    return "❌ TRADE FAILED after automatic re-approval. Ensure USDC.e is in your Safe."


def execute_sell_position(market_id: int, outcome: str, shares: float, address: str) -> str:
    """
    Sell (close) an existing position via CLOB API.
    Uses POST /order with a signed SELL market order (py_clob_client).
    For SELL: MarketOrderArgs.amount = shares to sell.
    Note: api.polymarket.us has /v1/order/close-position (marketSlug) but uses different auth.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.exceptions import PolyApiException

    m = market_cache.get(market_id)
    if not m:
        return f"Market #{market_id} not found in cache."
    raw = outcome.strip()
    if not raw:
        return "Invalid outcome (empty)."
    # Match outcome case-insensitively so "yes"/"Yes"/"YES" and any other outcome key work.
    outcome_lower = raw.lower()
    matched = next((o for o in m.outcomes if o.lower() == outcome_lower), None)
    if not matched:
        return f"Invalid outcome '{raw}'. Available: {', '.join(m.outcomes)}."
    outcome = matched
    try:
        shares_float = float(shares)
    except (ValueError, TypeError):
        return f"Invalid shares '{shares}'."
    if shares_float <= 0:
        return "Shares must be positive."
    side_idx = m.outcomes.index(outcome)
    token_id = m.clob_token_ids[side_idx]
    odds_cents = m.odds.get(outcome, 0)
    db_user = db.get_user_by_address(address)
    if not db_user:
        return "CRITICAL: Could not find user private key."

    user_id = db_user["user_id"]
    private_key = db_user["eth_private_key"]
    owner_addr = db_user.get("eth_address") or ""
    trading_addr = get_trading_wallet_address(owner_addr)
    use_safe = trading_addr and trading_addr.lower() != owner_addr.lower()

    if not (db_user.get("polymarket_approved") or 0):
        _run_gasless_approve(user_id, address)

    for attempt in range(2):
        try:
            # CLOB attributes orders to funder (Safe); must match wallet that holds the position.
            funder = trading_addr if use_safe else None
            logging.info(
                "Sell order: trading_addr=%s use_safe=%s token_id=%s amount=%.4f funder=%s",
                trading_addr, use_safe, token_id, shares_float, funder or "(EOA)",
            )
            # Per Polymarket skill: one client with funder, set_api_creds(create_or_derive_api_creds())
            client = ClobClient(
                host="https://clob.polymarket.com", chain_id=137, key=private_key,
                signature_type=2 if use_safe else 0,
                funder=funder,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            order_args = MarketOrderArgs(token_id=token_id, amount=shares_float, side="SELL")
            signed_order = client.create_market_order(order_args)
            resp = client.post_order(signed_order, orderType=OrderType.FOK)

            if isinstance(resp, dict):
                order_id = (
                    resp.get("orderID") or resp.get("orderId") or resp.get("order_id")
                    or resp.get("id") or resp.get("hash") or "N/A"
                )
                order_id = str(order_id).strip() if order_id else "N/A"
                status = resp.get("status") or resp.get("orderStatus") or "submitted"
                tx_hashes = resp.get("transactionsHashes") or resp.get("transactionHashes") or []
                tx_hash = (tx_hashes[0] if tx_hashes else None) or resp.get("transactHash") or resp.get("txHash") or "pending"
                tx_hash = str(tx_hash) if tx_hash else "pending"
            else:
                order_id = str(resp)
                status = "submitted"
                tx_hash = "pending"

            return (
                f"✅ POSITION CLOSED (sold {shares_float:.2f} {outcome})\n"
                f"Market: {m.question}\n"
                f"Order ID: {order_id}\n"
                f"Status: {status}\n"
                f"TX: {tx_hash}"
            )

        except PolyApiException as e:
            _log_clob_error(
                e,
                "post_order (sell)",
                address=address,
                request_ctx={
                    "token_id": token_id,
                    "amount_shares": shares_float,
                    "outcome": outcome,
                    "order_side": "SELL",
                    "trading_addr": trading_addr,
                    "use_safe": use_safe,
                    "funder": funder or "(EOA)",
                    "market_id": market_id,
                },
            )
            err = getattr(e, "error_message", {}) or {}
            msg = (err.get("error") or str(e)).lower()
            if attempt == 0 and ("allowance" in msg or "not enough balance" in msg):
                logging.warning(
                    "Sell failed with allowance error for trading_addr=%s; forcing re-approval and retrying...",
                    trading_addr,
                )
                _run_gasless_approve(user_id, address)
                time.sleep(5)  # allow approval tx to be mined before retry
                continue
            logging.error("Sell execution failed: %s", msg)
            return "❌ SELL FAILED: not enough balance or allowance."

        except Exception as e:
            logging.error("Sell execution failed: %s", e)
            return f"❌ SELL FAILED: {e}"

    return "❌ SELL FAILED after automatic re-approval."


@mcp.tool()
def search_news(query: str, max_results: int = 5) -> str:
    """Search for the latest global news articles about a specific topic to help forecast prediction market odds. Returns headlines, snippets, and publication dates via DuckDuckGo."""
    try:
        from ddgs import DDGS
        results: list[dict] = []
        seen_keys: set[tuple] = set()
        # Try progressively wider windows, but always keep newest first.
        # 'd' = last day, 'w' = last week, 'm' = last month, 'y' = last year.
        with DDGS() as ddgs:
            for timelimit in ("d", "w", "m", "y"):
                for r in ddgs.news(
                    query,
                    region="wt-wt",
                    safesearch="Off",
                    timelimit=timelimit,
                    max_results=max_results * 2,
                ):
                    key = (r.get("title", ""), r.get("date", ""))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    results.append({
                        "date": r.get("date", ""),
                        "title": r.get("title", ""),
                        "source": r.get("source", ""),
                        "snippet": r.get("body", "")
                    })
                    if len(results) >= max_results:
                        break
                if len(results) >= max_results:
                    break
                
        if not results:
            return f"No recent news found for query: '{query}'"
            
        return json.dumps(results, indent=2)
    except Exception as e:
        logging.error(f"Error fetching news for {query}: {e}")
        return f"Failed to fetch news. Error: {str(e)}"
