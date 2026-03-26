"""
Base chain USDC payment gate for AI market analysis.

Charges a configurable fee in USDC on Base (chain 8453) before allowing
analysis to proceed.  Inspired by the x402 (HTTP 402 Payment Required)
protocol for machine-to-machine micropayments.
"""

import json
import logging
import os
import time
from decimal import Decimal

from web3 import Web3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_CHAIN_ID = 8453
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_USDC_DECIMALS = 6
FEE_RECIPIENT = "0xfb7ed57A9892b31efCE6F56d1d4ECb4d4769b6AE"

# ---------------------------------------------------------------------------
# Configuration (env)
# ---------------------------------------------------------------------------
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
ANALYSIS_FEE_USDC = Decimal(os.getenv("ANALYSIS_FEE_USDC", "0.10"))
ANALYSIS_FEE_ENABLED = os.getenv("ANALYSIS_FEE_ENABLED", "1") == "1"

# ---------------------------------------------------------------------------
# Minimal ERC-20 ABI (balanceOf + transfer)
# ---------------------------------------------------------------------------
_ERC20_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],'
    '"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],'
    '"type":"function"},'
    '{"constant":false,"inputs":[{"name":"_to","type":"address"},'
    '{"name":"_value","type":"uint256"}],"name":"transfer",'
    '"outputs":[{"name":"","type":"bool"}],"type":"function"}]'
)

# ---------------------------------------------------------------------------
# Lazy Web3 singleton
# ---------------------------------------------------------------------------
_base_w3 = None


def _get_base_w3() -> Web3:
    global _base_w3
    if _base_w3 is None:
        _base_w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    return _base_w3


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_base_usdc_balance(address: str) -> Decimal:
    """Return USDC balance on Base for *address* in human-readable units."""
    w3 = _get_base_w3()
    checksum = Web3.to_checksum_address(address)
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(BASE_USDC_ADDRESS), abi=_ERC20_ABI
    )
    raw = usdc.functions.balanceOf(checksum).call()
    return Decimal(raw) / Decimal(10**BASE_USDC_DECIMALS)


def transfer_base_usdc(private_key: str, to_address: str, amount_usdc: Decimal) -> str:
    """
    Transfer *amount_usdc* USDC on Base from the account controlled by
    *private_key* to *to_address*.  Returns the transaction hash hex string.
    """
    w3 = _get_base_w3()
    acct = w3.eth.account.from_key(private_key)
    sender = acct.address

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(BASE_USDC_ADDRESS), abi=_ERC20_ABI
    )
    raw_amount = int(amount_usdc * Decimal(10**BASE_USDC_DECIMALS))

    nonce = w3.eth.get_transaction_count(sender)

    # EIP-1559 fees (Base L2)
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    max_priority = w3.to_wei(0.001, "gwei")
    max_fee = base_fee * 2 + max_priority

    tx = usdc.functions.transfer(
        Web3.to_checksum_address(to_address), raw_amount
    ).build_transaction(
        {
            "from": sender,
            "nonce": nonce,
            "gas": 65_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
            "chainId": BASE_CHAIN_ID,
        }
    )

    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("[AnalysisFee] USDC transfer TX sent: %s", tx_hash.hex())

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    if receipt.status != 1:
        raise RuntimeError(f"Fee transfer reverted (tx {tx_hash.hex()})")

    return tx_hash.hex()


# ---------------------------------------------------------------------------
# Main payment gate
# ---------------------------------------------------------------------------


def charge_analysis_fee(user_id: int, db) -> tuple[bool, str]:
    """
    Charge the analysis fee for *user_id*.

    Returns ``(True, tx_hash)`` on success or ``(False, error_message)`` on
    failure.  The caller can proceed with analysis only when the first element
    is ``True``.
    """
    if not ANALYSIS_FEE_ENABLED:
        return True, "fee_disabled"

    # Look up user wallet
    user = db.get_user(user_id)
    if not user:
        return False, "User account not found. Please /start the bot first."

    eth_address = user.get("eth_address")
    private_key = user.get("eth_private_key")
    if not eth_address or not private_key:
        return False, "Wallet not configured. Please contact support."

    # Check balance
    try:
        balance = get_base_usdc_balance(eth_address)
    except Exception as e:
        logger.error("[AnalysisFee] Balance check failed for %s: %s", eth_address[:10], e)
        return False, "Base chain RPC is temporarily unavailable. Please try again."

    if balance < ANALYSIS_FEE_USDC:
        return (
            False,
            f"Insufficient USDC on Base. You need at least {ANALYSIS_FEE_USDC} USDC.\n"
            f"Your wallet: {eth_address}\n"
            f"Current balance: {balance} USDC\n"
            f"Please fund your wallet with Base USDC and try again.",
        )

    # Transfer fee
    try:
        tx_hash = transfer_base_usdc(private_key, FEE_RECIPIENT, ANALYSIS_FEE_USDC)
    except Exception as e:
        error_str = str(e)
        logger.error("[AnalysisFee] Transfer failed for user %s: %s", user_id, error_str)
        if "insufficient funds" in error_str.lower():
            return (
                False,
                f"Insufficient ETH on Base for gas fees. "
                f"Send a small amount of ETH to your wallet: {eth_address}",
            )
        return False, "Fee payment transaction failed. Please try again."

    # Log to DB
    try:
        db.log_analysis_fee(
            user_id=user_id,
            amount_usdc=str(ANALYSIS_FEE_USDC),
            tx_hash=tx_hash,
            fee_recipient=FEE_RECIPIENT,
        )
    except Exception as e:
        logger.error("[AnalysisFee] Failed to log fee tx: %s", e)

    logger.info(
        "[AnalysisFee] Charged %s USDC from user %s (tx: %s)",
        ANALYSIS_FEE_USDC,
        user_id,
        tx_hash,
    )
    return True, tx_hash
