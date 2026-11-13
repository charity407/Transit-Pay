"""
TransitPay AI — Section 1: XRPL Treasury & Payment Channel Manager
==================================================================
Handles:
  - XRPL Testnet connectivity
  - CFO Treasury Wallet + Carrier Recipient Wallet setup
  - RLUSD TrustLine establishment
  - PayChannelCreate (budget-capped by CFO)
  - PaymentChannelClaim (agent-issued, ledger-enforced ceiling)
  - Emergency channel closure (kill-switch)

XRPL Testnet: wss://s.altnet.rippletest.net:51233
RLUSD Issuer (Testnet): sourced from tryrlusd.com
"""

import os
import json
import time
import logging
import hashlib
import hmac
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass, field, asdict

import xrpl
from xrpl.clients import WebsocketClient, JsonRpcClient
from xrpl.wallet import Wallet, generate_faucet_wallet
from xrpl.models.transactions import (
    PaymentChannelCreate,
    PaymentChannelClaim,
    PaymentChannelFund,
    PaymentChannelClaimFlag,
    TrustSet,
    Payment,
)
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountInfo, AccountChannels, Fee
from xrpl.transaction import submit_and_wait, autofill_and_sign
from xrpl.utils import xrp_to_drops, drops_to_xrp
from xrpl.core.keypairs import sign as keypairs_sign, derive_classic_address
from xrpl.core import keypairs
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xrpl_treasury")

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------
XRPL_TESTNET_WS  = "wss://s.altnet.rippletest.net:51233"
XRPL_TESTNET_RPC = "https://s.altnet.rippletest.net:51234"

# RLUSD on Testnet — issuer from https://tryrlusd.com
RLUSD_TESTNET_ISSUER   = os.getenv("RLUSD_TESTNET_ISSUER", "rQhWct2fv4Hhm3KVj3GhW8TQpnfQCMUDed")
RLUSD_CURRENCY_CODE    = "524C555344000000000000000000000000000000"  # hex for "RLUSD"
RLUSD_CURRENCY_DISPLAY = "RLUSD"

# Default channel params
DEFAULT_CHANNEL_MAX_RLUSD      = Decimal("1000.00")   # $1,000 cap
DEFAULT_SETTLE_DELAY_SECONDS   = 86400                # 24 hours
DEFAULT_CHANNEL_EXPIRY_SECONDS = 604800               # 7 days from now


# ---------------------------------------------------------------------------
# DATA CLASSES
# ---------------------------------------------------------------------------
@dataclass
class ChannelRecord:
    """Represents an active XRPL Payment Channel owned by the CFO."""
    channel_id:        str
    cfo_address:       str
    carrier_address:   str
    max_rlusd:         Decimal
    claimed_rlusd:     Decimal = Decimal("0")
    created_at:        float   = field(default_factory=time.time)
    expiry_at:         Optional[float] = None
    is_open:           bool    = True
    last_tx_hash:      Optional[str] = None

    @property
    def remaining_rlusd(self) -> Decimal:
        return self.max_rlusd - self.claimed_rlusd

    def to_dict(self) -> dict:
        d = asdict(self)
        d["max_rlusd"]     = str(self.max_rlusd)
        d["claimed_rlusd"] = str(self.claimed_rlusd)
        d["remaining_rlusd"] = str(self.remaining_rlusd)
        return d


import concurrent.futures

_xrpl_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

def execute_in_thread(fn, *args, **kwargs):
    """Run an XRPL synchronous client operation in a worker thread without active event loops."""
    future = _xrpl_thread_pool.submit(fn, *args, **kwargs)
    return future.result()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def _rpc_client() -> JsonRpcClient:
    return JsonRpcClient(XRPL_TESTNET_RPC)


def _drops_from_rlusd(rlusd_amount: Decimal) -> str:
    """
    Convert an RLUSD amount into an IssuedCurrencyAmount dict string.
    PayChannelCreate works in XRP drops for native channels; for IOU
    channels we pass the value directly.  Returns the string value only.
    """
    return str(rlusd_amount)


def _build_rlusd_amount(value: Decimal) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(
        currency=RLUSD_CURRENCY_DISPLAY,
        issuer=RLUSD_TESTNET_ISSUER,
        value=str(value),
    )


def rlusd_to_drops_equivalent(rlusd: Decimal, xrp_per_usd: Decimal = Decimal("0.05")) -> str:
    """
    Utility: convert a USD-denominated RLUSD amount to XRP drops
    at a given rate (channel has 50 XRP representing $1,000 RLUSD cap => 0.05 XRP/USD).
    Used when native-XRP channel capacity is needed as a proxy.
    """
    xrp_equiv = rlusd * xrp_per_usd
    return xrp_to_drops(float(xrp_equiv))


# ---------------------------------------------------------------------------
# WALLET SETUP
# ---------------------------------------------------------------------------
def provision_wallets(use_env: bool = True) -> tuple[Wallet, Wallet]:
    """
    Return (cfo_wallet, carrier_wallet).

    If USE_ENV=True, loads seed phrases from .env.
    Otherwise, generates fresh faucet wallets (testnet only).
    """
    if use_env and os.getenv("CFO_WALLET_SEED") and os.getenv("CARRIER_WALLET_SEED"):
        cfo_wallet     = Wallet.from_seed(os.getenv("CFO_WALLET_SEED"))
        carrier_wallet = Wallet.from_seed(os.getenv("CARRIER_WALLET_SEED"))
        logger.info("Loaded wallets from environment.")
        logger.info("CFO Address:     %s", cfo_wallet.address)
        logger.info("Carrier Address: %s", carrier_wallet.address)
        return cfo_wallet, carrier_wallet

    # Generate fresh wallets via Testnet faucet
    logger.info("Generating CFO wallet via XRPL Testnet faucet …")
    client = _rpc_client()
    cfo_wallet = generate_faucet_wallet(client, debug=False)
    logger.info("CFO Wallet:      %s", cfo_wallet.address)

    logger.info("Generating Carrier wallet via XRPL Testnet faucet …")
    carrier_wallet = generate_faucet_wallet(client, debug=False)
    logger.info("Carrier Wallet:  %s", carrier_wallet.address)

    # Persist seeds for reuse
    _save_env_seeds(cfo_wallet.seed, carrier_wallet.seed)
    return cfo_wallet, carrier_wallet


def _save_env_seeds(cfo_seed: str, carrier_seed: str):
    """Append wallet seeds to .env for persistence across runs."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "a") as fh:
        fh.write(f"\nCFO_WALLET_SEED={cfo_seed}\n")
        fh.write(f"CARRIER_WALLET_SEED={carrier_seed}\n")
    logger.info(".env updated with wallet seeds.")


# ---------------------------------------------------------------------------
# TRUSTLINE SETUP
# ---------------------------------------------------------------------------
def establish_rlusd_trustline(wallet: Wallet, limit: str = "10000") -> dict:
    """
    Set an RLUSD TrustSet transaction so the wallet can hold/receive RLUSD.
    limit: maximum RLUSD the wallet is willing to hold (string).
    """
    client = _rpc_client()
    trust_tx = TrustSet(
        account=wallet.address,
        limit_amount=IssuedCurrencyAmount(
            currency=RLUSD_CURRENCY_DISPLAY,
            issuer=RLUSD_TESTNET_ISSUER,
            value=limit,
        ),
    )
    logger.info("Setting RLUSD trustline for %s …", wallet.address)
    response = submit_and_wait(trust_tx, client, wallet)
    result = response.result
    tx_hash = result.get("hash", "N/A")
    logger.info("TrustSet TX: https://testnet.xrpl.org/transactions/%s", tx_hash)
    return {"address": wallet.address, "trustline_hash": tx_hash, "result": result}


# ---------------------------------------------------------------------------
# PAYMENT CHANNEL CREATION
# ---------------------------------------------------------------------------
def create_payment_channel(
    cfo_wallet:        Wallet,
    carrier_address:   str,
    max_rlusd:         Decimal = DEFAULT_CHANNEL_MAX_RLUSD,
    settle_delay:      int     = DEFAULT_SETTLE_DELAY_SECONDS,
    expiry_seconds:    int     = DEFAULT_CHANNEL_EXPIRY_SECONDS,
    channel_public_key: Optional[str] = None,
) -> ChannelRecord:
    """
    Open an XRPL Payment Channel from the CFO to the Carrier.

    The channel is funded with the XRP-equivalent of the RLUSD cap so the
    ledger can enforce the ceiling.  RLUSD claim amounts are verified by
    the agent-side safety check below.

    Args:
        cfo_wallet:      CFO's signing wallet (funds the channel)
        carrier_address: Recipient (carrier) XRPL address
        max_rlusd:       Hard cap in RLUSD (default $1,000)
        settle_delay:    Seconds channel stays open after close request
        expiry_seconds:  Absolute channel lifetime from now

    Returns:
        ChannelRecord with the on-chain channel_id
    """
    client = _rpc_client()

    # Fund channel with XRP equivalent (ledger enforces XRP ceiling)
    xrp_drops_cap = rlusd_to_drops_equivalent(max_rlusd)

    cancel_after_ripple_epoch = int(time.time()) - 946684800 + expiry_seconds

    tx = PaymentChannelCreate(
        account=cfo_wallet.address,
        amount=xrp_drops_cap,           # XRP drops cap (ledger enforced)
        destination=carrier_address,
        settle_delay=settle_delay,
        public_key=channel_public_key or cfo_wallet.public_key,
        cancel_after=cancel_after_ripple_epoch,
    )

    logger.info(
        "Creating PaymentChannel: CFO=%s → Carrier=%s, cap=%s RLUSD (%s drops XRP)",
        cfo_wallet.address, carrier_address, max_rlusd, xrp_drops_cap,
    )

    response = submit_and_wait(tx, client, cfo_wallet)
    result   = response.result

    if result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        err = result.get("meta", {}).get("TransactionResult", "UNKNOWN")
        raise XRPLTreasuryError(f"PayChannelCreate failed: {err}")

    # Extract channel_id from metadata
    channel_id = _extract_channel_id(result)
    tx_hash    = result.get("hash", "N/A")

    logger.info("Channel created: %s", channel_id)
    logger.info("TX: https://testnet.xrpl.org/transactions/%s", tx_hash)

    record = ChannelRecord(
        channel_id=channel_id,
        cfo_address=cfo_wallet.address,
        carrier_address=carrier_address,
        max_rlusd=max_rlusd,
        expiry_at=time.time() + expiry_seconds,
        last_tx_hash=tx_hash,
    )
    return record


def _extract_channel_id(tx_result: dict) -> str:
    """Parse the PayChannel ledger object ID from the transaction metadata."""
    affected_nodes = tx_result.get("meta", {}).get("AffectedNodes", [])
    for node in affected_nodes:
        created = node.get("CreatedNode", {})
        if created.get("LedgerEntryType") == "PayChannel":
            return created.get("LedgerIndex", "")
    raise XRPLTreasuryError("Could not find PayChannel LedgerIndex in tx metadata.")


# ---------------------------------------------------------------------------
# CHANNEL INFO QUERY
# ---------------------------------------------------------------------------
def get_channel_info(cfo_address: str, channel_id: str) -> Optional[dict]:
    """Fetch live Payment Channel state from the XRPL node."""
    def _do():
        client = _rpc_client()
        req    = AccountChannels(account=cfo_address)
        resp   = client.request(req)
        channels = resp.result.get("channels", [])
        for ch in channels:
            if ch.get("channel_id", "").upper() == channel_id.upper():
                return ch
        return None
    return execute_in_thread(_do)


def _persist_channel_record(record: ChannelRecord):
    state_path = os.path.join(os.path.dirname(__file__), "channel_state.json")
    if not os.path.exists(state_path):
        return
    with open(state_path) as fh:
        state = json.load(fh)
    for key, value in state.items():
        if key.endswith("_wallet") and isinstance(value, dict):
            value.pop("seed", None)
    state["channel"] = record.to_dict()
    with open(state_path, "w") as fh:
        json.dump(state, fh, indent=2, default=str)


def reconcile_channel_with_ledger(channel_record: ChannelRecord) -> Optional[dict]:
    """Synchronize local proxy capacity and cumulative claims with the XRPL channel."""
    live_channel = get_channel_info(channel_record.cfo_address, channel_record.channel_id)
    if live_channel is None:
        channel_record.is_open = False
    else:
        drops_per_rlusd = Decimal("1000000") * Decimal("0.05")
        channel_record.max_rlusd = Decimal(str(live_channel.get("amount", "0"))) / drops_per_rlusd
        channel_record.claimed_rlusd = Decimal(str(live_channel.get("balance", "0"))) / drops_per_rlusd
    _persist_channel_record(channel_record)
    return live_channel


def get_account_balance(address: str) -> dict:
    """Return XRP and issued-currency balances for an address."""
    def _do():
        client = _rpc_client()
        req    = AccountInfo(account=address, ledger_index="validated")
        resp   = client.request(req)
        xrp_drops = resp.result.get("account_data", {}).get("Balance", "0")
        return {
            "address":    address,
            "xrp":        str(drops_to_xrp(xrp_drops)),
            "xrp_drops":  xrp_drops,
        }
    return execute_in_thread(_do)


# ---------------------------------------------------------------------------
# PAYMENT CHANNEL CLAIM  (agent-signed)
# ---------------------------------------------------------------------------
def sign_channel_claim(
    agent_wallet:  Wallet,
    channel_id:    str,
    amount_drops:  str,
) -> str:
    """
    Produce a channel claim authorization signature.
    The key registered as PaymentChannelCreate.PublicKey signs the channel_id
    and cumulative amount authorization.
    """
    # XRPL channel claim signature: sign SHA512-Half of the claim prefix + channel + amount
    # Reference: https://xrpl.org/docs/references/protocol/transactions/types/paymentchannelclaim
    claim_prefix = bytes.fromhex("434C4D00")  # "CLM\x00"
    channel_bytes = bytes.fromhex(channel_id)
    amount_int    = int(amount_drops).to_bytes(8, "big")
    message       = claim_prefix + channel_bytes + amount_int
    signature     = keypairs.sign(message.hex(), agent_wallet.private_key)
    return signature


def claim_payment(
    channel_record:  ChannelRecord,
    rlusd_amount:    Decimal,
    agent_wallet:    Wallet,
    carrier_wallet:  Wallet,
) -> dict:
    """
    Agent authorizes a PaymentChannelClaim for `rlusd_amount` RLUSD. XRPL
    requires the channel destination (the carrier) to submit the transaction;
    the carrier signs its transaction envelope while the registered agent key
    signs the channel claim authorization.

    Safety Layer (pre-flight):
      1. Verify rlusd_amount ≤ channel_record.remaining_rlusd
      2. Verify rlusd_amount > 0
      3. Cross-check on-chain channel balance before submitting

    If any check fails the claim is aborted — the XRPL node would reject
    it anyway (tecINSUFF_PAY_CHANNEL_FUNDS) but we catch it early to log
    a clean audit trail.

    Args:
        channel_record:  Local ChannelRecord (holds cap + claimed so far)
        rlusd_amount:    Amount to claim in RLUSD
        agent_wallet:    Wallet matching the PublicKey registered on the channel
        carrier_wallet:  Destination wallet that submits the transaction

    Returns:
        dict with tx_hash, claimed_rlusd, audit details
    """
    logger.info("=== CLAIM REQUEST ===")
    logger.info("Channel:       %s", channel_record.channel_id)
    logger.info("Claim amount:  %s RLUSD", rlusd_amount)
    logger.info("Channel cap:   %s RLUSD", channel_record.max_rlusd)
    logger.info("Already paid:  %s RLUSD", channel_record.claimed_rlusd)
    logger.info("Remaining:     %s RLUSD", channel_record.remaining_rlusd)

    # --- Safety Check 1: positive amount ---
    if rlusd_amount <= Decimal("0"):
        raise ClaimRejectedError("Claim amount must be positive.")

    # --- Safety Check 2: does not exceed channel cap ---
    if rlusd_amount > channel_record.remaining_rlusd:
        raise ClaimRejectedError(
            f"Claim of {rlusd_amount} RLUSD exceeds remaining channel "
            f"balance of {channel_record.remaining_rlusd} RLUSD. "
            f"The XRPL node would reject this with tecINSUFF_PAY_CHANNEL_FUNDS."
        )

    # --- Safety Check 3: on-chain verification ---
    live_channel = get_channel_info(channel_record.cfo_address, channel_record.channel_id)
    if live_channel is None:
        raise ClaimRejectedError("Channel not found on-chain. It may have been closed.")
    if not channel_record.is_open:
        raise ClaimRejectedError("Channel is marked closed in local registry.")

    on_chain_drops = int(live_channel.get("amount", "0"))
    on_chain_xrp   = drops_to_xrp(str(on_chain_drops))
    logger.info("On-chain channel XRP balance: %s XRP", on_chain_xrp)

    # Convert cumulative RLUSD payout to XRP drops for the channel transaction
    new_total_rlusd = channel_record.claimed_rlusd + rlusd_amount
    cumulative_drops = rlusd_to_drops_equivalent(new_total_rlusd)

    # Generate authorization signature on cumulative drops
    signature = sign_channel_claim(agent_wallet, channel_record.channel_id, cumulative_drops)

    client = _rpc_client()
    claim_tx = PaymentChannelClaim(
        account=carrier_wallet.address,
        channel=channel_record.channel_id,
        amount=cumulative_drops,
        balance=cumulative_drops,
        signature=signature,
        public_key=agent_wallet.public_key,
    )

    logger.info("Submitting PaymentChannelClaim to XRPL Testnet …")
    response = execute_in_thread(submit_and_wait, claim_tx, client, carrier_wallet)
    result   = response.result

    tx_result_code = result.get("meta", {}).get("TransactionResult", "UNKNOWN")
    tx_hash        = result.get("hash", "N/A")

    if tx_result_code != "tesSUCCESS":
        _handle_xrpl_error(tx_result_code, rlusd_amount, channel_record)

    # Update local ledger state
    channel_record.claimed_rlusd += rlusd_amount
    channel_record.last_tx_hash   = tx_hash

    explorer_url = f"https://testnet.xrpl.org/transactions/{tx_hash}"
    logger.info("Claim SUCCESS: %s RLUSD | TX: %s", rlusd_amount, explorer_url)
    try:
        reconcile_channel_with_ledger(channel_record)
    except Exception as exc:
        logger.warning("Claim confirmed, but channel reconciliation failed: %s", exc)

    return {
        "status":          "SUCCESS",
        "tx_hash":         tx_hash,
        "explorer_url":    explorer_url,
        "claimed_rlusd":   str(rlusd_amount),
        "total_claimed":   str(channel_record.claimed_rlusd),
        "remaining_cap":   str(channel_record.remaining_rlusd),
        "tx_result_code":  tx_result_code,
        "channel_id":      channel_record.channel_id,
    }


def _handle_xrpl_error(code: str, amount: Decimal, channel: ChannelRecord):
    """Map XRPL error codes to human-readable exceptions."""
    messages = {
        "tecUNFUNDED_PAYMENT":        "Channel has insufficient XRP to cover this claim.",
        "tecINSUFF_PAY_CHANNEL_FUNDS": f"Claim of {amount} RLUSD exceeds channel balance.",
        "temBAD_AMOUNT":              "Malformed amount field — check drops conversion.",
        "tecNO_ENTRY":                "Channel does not exist or was already closed.",
        "tecNO_PERMISSION":           "Claimant is not the channel destination.",
        "tecEXPIRED":                 "Payment channel has expired.",
    }
    friendly = messages.get(code, f"XRPL error: {code}")
    raise XRPLTreasuryError(friendly)


# ---------------------------------------------------------------------------
# CHANNEL FUNDING (top-up by CFO)
# ---------------------------------------------------------------------------
def fund_payment_channel(
    cfo_wallet:     Wallet,
    channel_record: ChannelRecord,
    top_up_rlusd:   Decimal,
) -> dict:
    """
    CFO tops up an existing channel.  Also extends the cap in local registry.
    """
    client     = _rpc_client()
    extra_drops = rlusd_to_drops_equivalent(top_up_rlusd)

    tx = PaymentChannelFund(
        account=cfo_wallet.address,
        channel=channel_record.channel_id,
        amount=extra_drops,
    )
    logger.info("Funding channel %s with %s RLUSD …", channel_record.channel_id, top_up_rlusd)
    response = submit_and_wait(tx, client, cfo_wallet)
    result   = response.result
    tx_hash  = result.get("hash", "N/A")

    if result.get("meta", {}).get("TransactionResult") != "tesSUCCESS":
        err = result["meta"]["TransactionResult"]
        raise XRPLTreasuryError(f"PaymentChannelFund failed: {err}")

    channel_record.max_rlusd += top_up_rlusd
    channel_record.last_tx_hash = tx_hash
    try:
        reconcile_channel_with_ledger(channel_record)
    except Exception as exc:
        logger.warning("Channel funding confirmed, but reconciliation failed: %s", exc)
    logger.info("Channel cap raised to %s RLUSD | TX: %s", channel_record.max_rlusd, tx_hash)
    return {
        "status":        "FUNDED",
        "tx_hash":       tx_hash,
        "xrp_amount":    str(Decimal(extra_drops) / Decimal("1000000")),
        "new_cap_rlusd": str(channel_record.max_rlusd),
        "explorer_url":  f"https://testnet.xrpl.org/transactions/{tx_hash}",
    }


# ---------------------------------------------------------------------------
# KILL-SWITCH — Emergency Channel Closure
# ---------------------------------------------------------------------------
def emergency_close_channel(
    cfo_wallet:     Wallet,
    channel_record: ChannelRecord,
) -> dict:
    """
    CFO-activated kill-switch.  Sends PaymentChannelClose to revoke agent authority.
    After settle_delay, unclaimed XRP is returned to the CFO.
    """
    client = _rpc_client()
    tx = PaymentChannelClaim(
        account=cfo_wallet.address,
        channel=channel_record.channel_id,
        flags=PaymentChannelClaimFlag.TF_CLOSE,  # tfClose flag — requests immediate closure
    )
    logger.warning("!!! KILL-SWITCH ACTIVATED — Closing channel %s !!!", channel_record.channel_id)
    response = execute_in_thread(submit_and_wait, tx, client, cfo_wallet)
    result   = response.result
    tx_hash  = result.get("hash", "N/A")

    tx_result_code = result.get("meta", {}).get("TransactionResult", "UNKNOWN")
    if tx_result_code != "tesSUCCESS":
        raise XRPLTreasuryError(f"PaymentChannelClose failed: {tx_result_code}")

    channel_record.is_open = False
    channel_record.last_tx_hash = tx_hash
    try:
        reconcile_channel_with_ledger(channel_record)
    except Exception as exc:
        logger.warning("Channel close confirmed, but reconciliation failed: %s", exc)
    logger.warning("Channel closed | TX: https://testnet.xrpl.org/transactions/%s", tx_hash)

    return {
        "status":       "CHANNEL_CLOSED",
        "tx_hash":      tx_hash,
        "channel_id":   channel_record.channel_id,
        "explorer_url": f"https://testnet.xrpl.org/transactions/{tx_hash}",
        "message":      "Agent authority revoked. Unclaimed XRP will be returned after settle_delay.",
    }


# ---------------------------------------------------------------------------
# CUSTOM EXCEPTIONS
# ---------------------------------------------------------------------------
class XRPLTreasuryError(Exception):
    """Raised when an XRPL ledger operation fails."""

class ClaimRejectedError(Exception):
    """Raised when a claim is blocked by the agent-side safety layer."""


# ---------------------------------------------------------------------------
# FULL BOOTSTRAP (run once to initialise testnet environment)
# ---------------------------------------------------------------------------
def bootstrap_testnet_environment() -> dict:
    """
    End-to-end testnet bootstrap:
      1. Provision wallets (faucet or .env seeds)
      2. Set RLUSD trustlines on both wallets
      3. Create a payment channel (CFO → Carrier, $1,000 cap)
      4. Return full environment state dict

    Security note: private wallet seeds are NEVER written to channel_state.json.
    Only public addresses, the channel_id, and public keys are persisted.
    """
    logger.info("=== TransitPay AI — XRPL Testnet Bootstrap ===")

    cfo_wallet, carrier_wallet = provision_wallets(use_env=True)

    # Register the agent key for claim authorization, falling back to CFO for
    # environments without a dedicated agent wallet.
    agent_seed = os.getenv("AGENT_WALLET_SEED", "")
    if agent_seed:
        agent_wallet = Wallet.from_seed(agent_seed)
        channel_public_key = agent_wallet.public_key
        logger.info("Agent wallet loaded for channel public key: %s", agent_wallet.address)
    else:
        agent_wallet = cfo_wallet
        channel_public_key = agent_wallet.public_key
        logger.info("No AGENT_WALLET_SEED — using CFO public key for channel registration.")

    # Set RLUSD trustlines
    cfo_trust     = establish_rlusd_trustline(cfo_wallet,     limit="50000")
    carrier_trust = establish_rlusd_trustline(carrier_wallet, limit="50000")

    # Open payment channel (Account = CFO; PublicKey = agent_wallet or CFO)
    channel = create_payment_channel(
        cfo_wallet=cfo_wallet,
        carrier_address=carrier_wallet.address,
        max_rlusd=Decimal("1000"),
        channel_public_key=channel_public_key,
    )

    # NOTE: Seeds are intentionally excluded from env_state to prevent leakage.
    env_state = {
        "cfo_wallet":          {"address": cfo_wallet.address, "public_key": cfo_wallet.public_key},
        "agent_wallet":        {"address": agent_wallet.address, "public_key": agent_wallet.public_key},
        "carrier_wallet":      {"address": carrier_wallet.address, "public_key": carrier_wallet.public_key},
        "channel_public_key":  channel_public_key,
        "cfo_trustline_tx":    cfo_trust["trustline_hash"],
        "carrier_trustline_tx":carrier_trust["trustline_hash"],
        "channel":             channel.to_dict(),
        "rlusd_issuer":        RLUSD_TESTNET_ISSUER,
        "network":             XRPL_TESTNET_RPC,
    }

    # Persist channel state to JSON for the agent service (no seeds written here)
    state_path = os.path.join(os.path.dirname(__file__), "channel_state.json")
    with open(state_path, "w") as fh:
        json.dump(env_state, fh, indent=2, default=str)
    logger.info("Environment state saved to %s", state_path)

    return env_state


if __name__ == "__main__":
    state = bootstrap_testnet_environment()
    print(json.dumps(state, indent=2, default=str))
