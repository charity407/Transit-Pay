"""
TransitPay AI — Section 2: Autonomous Agent Brain
==================================================
FastAPI service that:
  1. Ingests a Proof-of-Delivery (POD) document (image or text)
  2. Ingests GPS Telemetry JSON for geofence dwell-time analysis
  3. Uses OpenAI Vision to parse POD fields (arrival/departure/freight ID/carrier wallet)
  4. Computes demurrage: free_hours = 2, rate = $20/hr
  5. Builds and signs a PaymentChannelClaim via xrpl_treasury.py
  6. Returns a full audit log JSON with XRPL explorer link

Endpoints:
  POST /api/v1/process-delivery   — full autonomous payout pipeline
  GET  /api/v1/audit-logs         — paginated audit history
  GET  /api/v1/channel-status     — live channel state
  POST /api/v1/channel-fund       — CFO adds funds to the active channel
  POST /api/v1/update-limits      — CFO updates agent limits at runtime
  POST /api/v1/kill-switch        — CFO emergency channel close
"""

import os
import json
import uuid
import base64
import logging
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Local treasury module
from xrpl_treasury import (
    ChannelRecord,
    ClaimRejectedError,
    XRPLTreasuryError,
    claim_payment,
    emergency_close_channel,
    fund_payment_channel,
    get_channel_info,
    get_account_balance,
    RLUSD_TESTNET_ISSUER,
    XRPL_TESTNET_RPC,
)
from xrpl.wallet import Wallet

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent_service")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
AGENT_WALLET_SEED    = os.getenv("AGENT_WALLET_SEED", "")     # agent-specific wallet (not CFO)
CARRIER_WALLET_SEED  = os.getenv("CARRIER_WALLET_SEED", "")
CFO_WALLET_SEED      = os.getenv("CFO_WALLET_SEED", "")
CHANNEL_STATE_FILE   = Path(__file__).parent / "channel_state.json"
AUDIT_LOG_FILE       = Path(__file__).parent / "audit_logs.jsonl"

# Demurrage policy (overridable by CFO via /update-limits)
AGENT_LIMITS = {
    "base_freight_usd":        Decimal("500.00"),
    "free_hours":              2,
    "demurrage_rate_per_hour": Decimal("20.00"),
    "max_single_trip_rlusd":   Decimal("1000.00"),
    "max_daily_rlusd":         Decimal("5000.00"),
    "demurrage_rate_cap":      Decimal("50.00"),   # max $/hr the agent will approve
    "autonomous_mode":         True,               # False = Shadow Mode (recommend only)
}

daily_spend_tracker: dict[str, Decimal] = {}       # date_str → total_spent
audit_logs: list[dict] = []

# ---------------------------------------------------------------------------
# PYDANTIC SCHEMAS
# ---------------------------------------------------------------------------
class GPSTelemetry(BaseModel):
    freight_id:          str
    truck_plate:         str
    geofence_name:       str
    entry_timestamp:     str    # ISO 8601
    exit_timestamp:      str    # ISO 8601
    total_dwell_minutes: int
    geofence_lat:        float
    geofence_lon:        float
    carrier_wallet:      str    # XRPL address of carrier

class DeliveryRequest(BaseModel):
    gps_telemetry:       GPSTelemetry
    pod_text:            Optional[str] = None   # pre-parsed POD text (fallback if no image)

class AgentLimitsUpdate(BaseModel):
    base_freight_usd:        Optional[float] = None
    free_hours:              Optional[int]   = None
    demurrage_rate_per_hour: Optional[float] = None
    max_single_trip_rlusd:   Optional[float] = None
    max_daily_rlusd:         Optional[float] = None
    demurrage_rate_cap:      Optional[float] = None
    autonomous_mode:         Optional[bool]  = None

class ChannelFundingRequest(BaseModel):
    amount_rlusd: Decimal = Field(gt=0, max_digits=12, decimal_places=2)

class AuditLog(BaseModel):
    log_id:              str
    timestamp:           str
    freight_id:          str
    truck_plate:         str
    carrier_wallet:      str
    base_freight_rlusd:  str
    dwell_hours:         float
    free_hours:          int
    billable_hours:      float
    demurrage_rlusd:     str
    total_payout_rlusd:  str
    kes_equivalent:      Optional[str] = None
    channel_id:          str
    channel_cap_rlusd:   str
    channel_remaining:   str
    decision:            str    # "APPROVED" | "REJECTED" | "SHADOW_MODE"
    rejection_code:      Optional[str] = None
    rejection_reason:    Optional[str] = None
    tx_hash:             Optional[str] = None
    explorer_url:        Optional[str] = None
    reasoning:           str    # LLM chain-of-thought
    pod_parsed_fields:   dict

# ---------------------------------------------------------------------------
# APP INIT
# ---------------------------------------------------------------------------
app = FastAPI(
    title="TransitPay AI — Agent Service",
    description="Autonomous freight & demurrage settlement agent powered by XRPL + RLUSD",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY or "sk-dummy-key-for-local-demo")


# ---------------------------------------------------------------------------
# HELPERS — Channel State Loader
# ---------------------------------------------------------------------------
def _load_channel_record() -> ChannelRecord:
    """Load the active payment channel from the persisted JSON state file."""
    if not CHANNEL_STATE_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail="Channel state file not found. Run xrpl_treasury.py bootstrap first.",
        )
    with open(CHANNEL_STATE_FILE) as fh:
        state = json.load(fh)

    ch = state["channel"]
    return ChannelRecord(
        channel_id=ch["channel_id"],
        cfo_address=ch["cfo_address"],
        carrier_address=ch["carrier_address"],
        max_rlusd=Decimal(ch["max_rlusd"]),
        claimed_rlusd=Decimal(ch["claimed_rlusd"]),
        is_open=ch["is_open"],
        last_tx_hash=ch.get("last_tx_hash"),
    )


def _save_channel_record(record: ChannelRecord):
    """Persist updated channel record back to JSON state file."""
    with open(CHANNEL_STATE_FILE, "r") as fh:
        state = json.load(fh)
    state["channel"] = record.to_dict()
    with open(CHANNEL_STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2, default=str)


def _load_wallets() -> tuple[Wallet, Wallet]:
    agent_seed   = AGENT_WALLET_SEED or CFO_WALLET_SEED   # agent uses CFO seed if no dedicated seed
    carrier_seed = CARRIER_WALLET_SEED
    if not agent_seed or not carrier_seed:
        raise HTTPException(status_code=503, detail="Wallet seeds not configured in .env")
    return Wallet.from_seed(agent_seed), Wallet.from_seed(carrier_seed)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_budget_check(amount: Decimal):
    today   = _today_key()
    spent   = daily_spend_tracker.get(today, Decimal("0"))
    limit   = AGENT_LIMITS["max_daily_rlusd"]
    if spent + amount > limit:
        raise ClaimRejectedError(
            f"Daily budget exceeded. Spent today: {spent} RLUSD, "
            f"limit: {limit} RLUSD, requested: {amount} RLUSD."
        )


def _record_daily_spend(amount: Decimal):
    today = _today_key()
    daily_spend_tracker[today] = daily_spend_tracker.get(today, Decimal("0")) + amount


def _append_audit_log(log: dict):
    audit_logs.append(log)
    with open(AUDIT_LOG_FILE, "a") as fh:
        fh.write(json.dumps(log, default=str) + "\n")


# ---------------------------------------------------------------------------
# STEP 1 — POD Vision Parsing
# ---------------------------------------------------------------------------
MOCK_POD_TEXT = """
PROOF OF DELIVERY
=================
Freight ID     : FRT-2024-KLA-0042
Carrier        : Mombasa Express Logistics Ltd.
Carrier Wallet : {carrier_wallet}
Origin         : Mombasa Port Gate 7
Destination    : Kampala Central Warehouse, Plot 14 Jinja Road
Arrival Time   : 2024-07-15T08:30:00Z
Departure Time : 2024-07-15T16:45:00Z
Goods          : 500 cartons Unilever FMCG (12,500 kg)
Delivery Status: COMPLETED
Receiver Sig   : J. Ochieng (Warehouse Manager)
"""

async def parse_pod_with_vision(
    pod_image_b64: Optional[str],
    pod_text: Optional[str],
    carrier_wallet: str,
    freight_id: Optional[str] = None,
    dwell_minutes: int = 210,
    route_name: Optional[str] = None,
) -> dict:
    """
    Use OpenAI GPT-4o Vision to extract structured fields and OCR bounding boxes from a POD document.
    Falls back to dynamic mock parse with realistic OCR bounding boxes if no API key is provided.
    """
    if not OPENAI_API_KEY or OPENAI_API_KEY.startswith("sk-your"):
        logger.warning("No OpenAI API key — using dynamic mock POD data with OCR bounding boxes.")
        return _mock_pod_parse(carrier_wallet, freight_id=freight_id, pod_text=pod_text, dwell_minutes=dwell_minutes, route_name=route_name)

    system_prompt = """
    You are a logistics document parser. Extract structured JSON from a Proof of Delivery (POD).
    Return ONLY valid JSON with these fields:
    {
      "freight_id": "string",
      "arrival_time": "ISO8601 string",
      "departure_time": "ISO8601 string",
      "carrier_name": "string",
      "carrier_wallet": "XRPL address or extracted from text",
      "origin": "string",
      "destination": "string",
      "goods_description": "string",
      "delivery_status": "COMPLETED|PARTIAL|REJECTED",
      "receiver_signature": "string or null",
      "confidence_score": 0.0-1.0,
      "ocr_bounding_boxes": [
        {"field": "string", "label": "string", "value": "string", "confidence": 0.99, "box": [top, left, width, height]}
      ]
    }
    If a field is missing, use null. Never add commentary outside the JSON.
    """

    messages = [{"role": "system", "content": system_prompt}]

    if pod_image_b64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Parse this scanned POD document image and extract all stamped arrival/departure times, freight numbers, and signatures with OCR coordinates:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{pod_image_b64}"}},
            ],
        })
    else:
        text = pod_text or MOCK_POD_TEXT.format(carrier_wallet=carrier_wallet)
        messages.append({"role": "user", "content": f"Parse this POD text:\n\n{text}"})

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content)
        # Override carrier_wallet with GPS telemetry value (ground truth)
        parsed["carrier_wallet"] = carrier_wallet
        if not parsed.get("ocr_bounding_boxes"):
            mock_boxes = _mock_pod_parse(carrier_wallet, freight_id=freight_id, pod_text=pod_text, dwell_minutes=dwell_minutes, route_name=route_name)
            parsed["ocr_bounding_boxes"] = mock_boxes["ocr_bounding_boxes"]
        return parsed
    except Exception as exc:
        logger.error("Vision API error: %s — falling back to mock", exc)
        return _mock_pod_parse(carrier_wallet, freight_id=freight_id, pod_text=pod_text, dwell_minutes=dwell_minutes, route_name=route_name)


def _mock_pod_parse(
    carrier_wallet: str,
    freight_id: Optional[str] = None,
    pod_text: Optional[str] = None,
    dwell_minutes: int = 210,
    route_name: Optional[str] = None,
) -> dict:
    """Return deterministic, high-fidelity mock POD parse with OCR bounding boxes for hackathon demos."""
    hours = dwell_minutes // 60
    mins = dwell_minutes % 60
    arrival_hhmm = "08:30 AM"
    
    dep_total_mins = 8 * 60 + 30 + dwell_minutes
    dep_h = (dep_total_mins // 60) % 24
    dep_m = dep_total_mins % 60
    dep_ampm = "PM" if dep_h >= 12 else "AM"
    dep_display_h = dep_h if dep_h <= 12 else dep_h - 12
    if dep_display_h == 0:
        dep_display_h = 12
    departure_hhmm = f"{dep_display_h:02d}:{dep_m:02d} {dep_ampm}"

    fid = freight_id or "FRT-2024-KLA-0042"
    if pod_text and "Freight:" in pod_text:
        for line in pod_text.splitlines():
            if "Freight:" in line:
                extracted = line.split("Freight:")[1].strip()
                if extracted:
                    fid = extracted
                break

    origin = "Mombasa Port Gate 7"
    destination = "Kampala Central Warehouse"
    if route_name and "->" in route_name:
        parts = route_name.split("->")
        origin = parts[0].strip()
        destination = parts[1].strip()

    return {
        "freight_id":        fid,
        "arrival_time":      f"2024-07-15T08:30:00Z ({arrival_hhmm})",
        "departure_time":    f"2024-07-15T{dep_h:02d}:{dep_m:02d}:00Z ({departure_hhmm})",
        "carrier_name":      "Mombasa Express Logistics Ltd.",
        "carrier_wallet":    carrier_wallet,
        "origin":            origin,
        "destination":       destination,
        "goods_description": "500 cartons Unilever FMCG (12,500 kg)",
        "delivery_status":   "COMPLETED",
        "receiver_signature":"J. Ochieng (Warehouse Manager)",
        "confidence_score":  0.985,
        "ocr_bounding_boxes": [
            {
                "field": "freight_id",
                "label": "CONSIGNMENT WAYBILL ID",
                "value": fid,
                "confidence": 0.994,
                "box": [42, 110, 240, 32],
            },
            {
                "field": "arrival_stamp",
                "label": "GATE-IN PHYSICAL STAMP",
                "value": f"ARRIVED: {arrival_hhmm}",
                "confidence": 0.988,
                "box": [310, 250, 210, 52],
            },
            {
                "field": "departure_stamp",
                "label": "DEPOT DISPATCH STAMP",
                "value": f"DEPARTED: {departure_hhmm}",
                "confidence": 0.991,
                "box": [310, 315, 210, 52],
            },
            {
                "field": "carrier_wallet",
                "label": "XRPL RECIPIENT WALLET",
                "value": carrier_wallet,
                "confidence": 0.978,
                "box": [42, 210, 320, 28],
            },
            {
                "field": "receiver_signature",
                "label": "VERIFIED CONSIGNEE SIGNATURE",
                "value": "J. Ochieng (Warehouse Manager)",
                "confidence": 0.965,
                "box": [310, 415, 210, 48],
            },
        ],
    }


# ---------------------------------------------------------------------------
# STEP 2 — Demurrage Calculation Engine
# ---------------------------------------------------------------------------
def calculate_demurrage(dwell_minutes: int) -> dict:
    """
    Compute demurrage charge based on dwell time vs. free hours policy.

    Policy:
      - First `free_hours` are free (included in base freight).
      - Each additional hour (or partial hour) is billed at `demurrage_rate_per_hour`.
      - Demurrage rate cannot exceed `demurrage_rate_cap`.

    Args:
        dwell_minutes: Total minutes truck was inside the geofence.

    Returns:
        dict with full calculation breakdown.
    """
    free_hours              = AGENT_LIMITS["free_hours"]
    rate_per_hour           = min(
        AGENT_LIMITS["demurrage_rate_per_hour"],
        AGENT_LIMITS["demurrage_rate_cap"],
    )
    base_freight            = AGENT_LIMITS["base_freight_usd"]

    dwell_hours             = Decimal(str(dwell_minutes)) / Decimal("60")
    billable_hours          = max(Decimal("0"), dwell_hours - Decimal(str(free_hours)))
    demurrage_charge        = (billable_hours * rate_per_hour).quantize(Decimal("0.01"))
    total_payout            = (base_freight + demurrage_charge).quantize(Decimal("0.01"))

    reasoning = (
        f"Dwell time: {float(dwell_hours):.2f} hrs. "
        f"Free allowance: {free_hours} hrs. "
        f"Billable: {float(billable_hours):.2f} hrs × ${float(rate_per_hour)}/hr = "
        f"${float(demurrage_charge)} demurrage. "
        f"Base freight: ${float(base_freight)}. "
        f"Total payout: ${float(total_payout)} RLUSD."
    )

    return {
        "dwell_hours":        float(dwell_hours),
        "free_hours":         free_hours,
        "billable_hours":     float(billable_hours),
        "demurrage_rate":     float(rate_per_hour),
        "base_freight_rlusd": str(base_freight),
        "demurrage_rlusd":    str(demurrage_charge),
        "total_payout_rlusd": str(total_payout),
        "reasoning":          reasoning,
    }


# ---------------------------------------------------------------------------
# STEP 3 — LLM Authorisation Reasoning (chain-of-thought)
# ---------------------------------------------------------------------------
async def llm_authorise_payout(pod_fields: dict, calc: dict, channel: ChannelRecord) -> dict:
    """
    Ask the LLM to reason about whether the payout is legitimate
    and produce a structured authorisation decision.
    Returns {"approved": bool, "reasoning": str, "flags": list[str]}
    """
    if not OPENAI_API_KEY or OPENAI_API_KEY.startswith("sk-your"):
        return _mock_authorisation(calc, channel)

    prompt = f"""
You are a treasury compliance AI for a freight company.
Review the following delivery event and decide if the calculated payout is legitimate.

## Proof of Delivery Fields
{json.dumps(pod_fields, indent=2)}

## Demurrage Calculation
{json.dumps(calc, indent=2)}

## Payment Channel Status
- Channel ID:   {channel.channel_id}
- Max Cap:      {channel.max_rlusd} RLUSD
- Already Paid: {channel.claimed_rlusd} RLUSD
- Remaining:    {channel.remaining_rlusd} RLUSD
- Requested:    {calc['total_payout_rlusd']} RLUSD

## Authorisation Rules
1. Delivery status must be COMPLETED.
2. Total payout must not exceed channel remaining balance.
3. Confidence score from POD parse must be ≥ 0.80.
4. Demurrage rate must not exceed policy cap.
5. Freight ID must be a valid non-null string.

Respond with ONLY valid JSON:
{{
  "approved": true|false,
  "reasoning": "step-by-step explanation",
  "flags": ["list of compliance flags if any"]
}}
"""
    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(response.choices[0].message.content)


def _mock_authorisation(calc: dict, channel: ChannelRecord) -> dict:
    total = Decimal(calc["total_payout_rlusd"])
    max_trip = AGENT_LIMITS["max_single_trip_rlusd"]
    flags = []

    if not channel.is_open:
        flags.append("CHANNEL_CLOSED_KILL_SWITCH")
        return {
            "approved": False,
            "rejection_code": "tecNO_ENTRY",
            "reasoning": "Rejected: tecNO_ENTRY (Payment channel closed via CFO Emergency Kill-Switch). Agent authority revoked.",
            "flags": flags,
        }

    if total > channel.remaining_rlusd:
        flags.append("EXCEEDS_CHANNEL_CAP")
    if total > max_trip:
        flags.append("EXCEEDS_SINGLE_TRIP_CAP")

    if "EXCEEDS_CHANNEL_CAP" in flags:
        return {
            "approved": False,
            "rejection_code": "tecUNFUNDED_PAYMENT",
            "reasoning": f"Rejected: tecUNFUNDED_PAYMENT (Claim of ${float(total):.2f} exceeds active channel balance of ${float(channel.remaining_rlusd):.2f} RLUSD).",
            "flags": flags,
        }

    if "EXCEEDS_SINGLE_TRIP_CAP" in flags:
        return {
            "approved": False,
            "rejection_code": "POLICY_TRIP_CAP",
            "reasoning": f"Rejected: Exceeded ${float(max_trip):.2f} Trip Cap (Calculated payout: ${float(total):.2f} RLUSD). Blocked by policy governance.",
            "flags": flags,
        }

    return {
        "approved": True,
        "rejection_code": None,
        "reasoning": (
            f"{calc['reasoning']} Payout of ${float(total):.2f} RLUSD is within single-trip cap (${float(max_trip):.2f}) "
            f"and active channel float (${float(channel.remaining_rlusd):.2f} RLUSD). Approved for XRPL settlement."
        ),
        "flags": [],
    }


# ---------------------------------------------------------------------------
# MAIN ENDPOINT — Process Delivery
# ---------------------------------------------------------------------------
@app.post("/api/v1/process-delivery", response_model=AuditLog)
async def process_delivery(
    request:          Request,
    background_tasks: BackgroundTasks,
    gps_data:         Optional[str]        = Form(None, description="JSON string of GPSTelemetry"),
    pod_image:        Optional[UploadFile] = File(None, description="POD receipt image"),
    pod_text:         Optional[str]        = Form(None, description="Pre-parsed POD text"),
):
    """
    Main agentic pipeline:
      1. Parse GPS telemetry (from JSON body or multipart form)
      2. Parse POD (image or text) via Vision API
      3. Calculate demurrage
      4. LLM authorisation check
      5. Submit PaymentChannelClaim to XRPL Testnet
      6. Return full audit log
    """
    log_id    = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # --- Parse GPS & input parameters (support both JSON body and Form data) ---
    pod_image_b64 = None
    parsed_gps = None
    incoming_pod_text = pod_text

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            if "gps_data" in body and isinstance(body["gps_data"], dict):
                parsed_gps = GPSTelemetry(**body["gps_data"])
            elif "freight_id" in body and "truck_plate" in body:
                parsed_gps = GPSTelemetry(**body)
            elif "gps_telemetry" in body:
                parsed_gps = GPSTelemetry(**body["gps_telemetry"])
            incoming_pod_text = body.get("pod_text")
            pod_image_b64 = body.get("pod_image_b64")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}")
    else:
        if gps_data:
            try:
                parsed_gps = GPSTelemetry(**json.loads(gps_data))
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Invalid GPS telemetry JSON: {exc}")
        if pod_image:
            raw_bytes = await pod_image.read()
            pod_image_b64 = base64.b64encode(raw_bytes).decode()

    if not parsed_gps:
        # Default fallback scenario if minimal body sent
        parsed_gps = GPSTelemetry(
            freight_id="FRT-2024-KLA-0042",
            truck_plate="KAA-123K",
            geofence_name="Kampala Central Warehouse",
            entry_timestamp=datetime.now(timezone.utc).isoformat(),
            exit_timestamp=datetime.now(timezone.utc).isoformat(),
            total_dwell_minutes=330,
            geofence_lat=0.3476,
            geofence_lon=32.5825,
            carrier_wallet="rLytz9wbRUkJnBDL21SmSSvDkMY6J4rXRp",
        )

    gps = parsed_gps

    # --- Load channel ---
    channel_record = _load_channel_record()
    agent_wallet, carrier_wallet = _load_wallets()

    # --- Parse POD via Vision ---
    pod_fields = await parse_pod_with_vision(
        pod_image_b64,
        incoming_pod_text,
        gps.carrier_wallet,
        freight_id=gps.freight_id,
        dwell_minutes=gps.total_dwell_minutes,
        route_name=gps.geofence_name,
    )

    # --- Demurrage Calculation ---
    calc = calculate_demurrage(gps.total_dwell_minutes)
    total_payout = Decimal(calc["total_payout_rlusd"])
    kes_equivalent = f"~{int(float(total_payout) * 129):,} KES"

    # --- Authorisation ---
    auth = await llm_authorise_payout(pod_fields, calc, channel_record)

    decision         = "SHADOW_MODE"
    rejection_code   = auth.get("rejection_code")
    rejection_reason = None
    tx_hash          = None
    explorer_url     = None

    if not auth["approved"]:
        decision         = "REJECTED"
        rejection_reason = auth["reasoning"]
        logger.warning("Payout REJECTED for %s: %s", gps.freight_id, rejection_reason)
    elif not AGENT_LIMITS["autonomous_mode"]:
        decision = "SHADOW_MODE"
        logger.info("Shadow mode: payout of %s RLUSD recommended but NOT executed.", total_payout)
    else:
        # --- Execute XRPL Claim ---
        try:
            _daily_budget_check(total_payout)
            claim_result = await asyncio.to_thread(
                claim_payment,
                channel_record=channel_record,
                rlusd_amount=total_payout,
                agent_wallet=agent_wallet,
                carrier_wallet=carrier_wallet,
            )
            tx_hash      = claim_result["tx_hash"]
            explorer_url = claim_result["explorer_url"]
            decision     = "APPROVED"
            _record_daily_spend(total_payout)
            _save_channel_record(channel_record)
        except ClaimRejectedError as exc:
            decision         = "REJECTED"
            err_msg          = str(exc)
            rejection_reason = err_msg
            if "tecINSUFF_PAY_CHANNEL_FUNDS" in err_msg or "exceeds remaining channel" in err_msg:
                rejection_code = "tecUNFUNDED_PAYMENT"
            elif "Daily budget" in err_msg:
                rejection_code = "POLICY_DAILY_BUDGET"
            elif "closed" in err_msg:
                rejection_code = "tecNO_ENTRY"
            else:
                rejection_code = "CLAIM_REJECTED"
            logger.error("Claim rejected: %s", exc)
        except XRPLTreasuryError as exc:
            decision         = "REJECTED"
            rejection_reason = f"XRPL error: {exc}"
            rejection_code   = "tecUNFUNDED_PAYMENT"
            logger.error("XRPL treasury error: %s", exc)

    # --- Compile Audit Log ---
    log_entry = {
        "log_id":              log_id,
        "timestamp":           timestamp,
        "freight_id":          gps.freight_id,
        "truck_plate":         gps.truck_plate,
        "carrier_wallet":      gps.carrier_wallet,
        "base_freight_rlusd":  calc["base_freight_rlusd"],
        "dwell_hours":         calc["dwell_hours"],
        "free_hours":          calc["free_hours"],
        "billable_hours":      calc["billable_hours"],
        "demurrage_rlusd":     calc["demurrage_rlusd"],
        "total_payout_rlusd":  calc["total_payout_rlusd"],
        "kes_equivalent":      kes_equivalent,
        "channel_id":          channel_record.channel_id,
        "channel_cap_rlusd":   str(channel_record.max_rlusd),
        "channel_remaining":   str(channel_record.remaining_rlusd),
        "decision":            decision,
        "rejection_code":      rejection_code,
        "rejection_reason":    rejection_reason,
        "tx_hash":             tx_hash,
        "explorer_url":        explorer_url,
        "reasoning":           auth["reasoning"],
        "pod_parsed_fields":   pod_fields,
    }

    background_tasks.add_task(_append_audit_log, log_entry)
    logger.info("Audit log written: %s | Decision: %s", log_id, decision)
    return JSONResponse(content=log_entry)

    background_tasks.add_task(_append_audit_log, log_entry)
    logger.info("Audit log written: %s | Decision: %s", log_id, decision)
    return JSONResponse(content=log_entry)


# ---------------------------------------------------------------------------
# ENDPOINT — Audit Logs
# ---------------------------------------------------------------------------
@app.get("/api/v1/audit-logs")
async def get_audit_logs(limit: int = 50, offset: int = 0):
    page = audit_logs[offset: offset + limit]
    return {"total": len(audit_logs), "logs": page}


# ---------------------------------------------------------------------------
# ENDPOINT — Channel Status
# ---------------------------------------------------------------------------
@app.get("/api/v1/channel-status")
def channel_status():
    record = _load_channel_record()
    try:
        live = get_channel_info(record.cfo_address, record.channel_id)
    except Exception as exc:
        logger.warning("Could not fetch live channel info: %s", exc)
        live = None

    # Read state file for wallet addresses
    with open(CHANNEL_STATE_FILE) as fh:
        state = json.load(fh)

    try:
        cfo_balance = get_account_balance(state["cfo_wallet"]["address"])
    except Exception:
        cfo_balance = {"address": state["cfo_wallet"]["address"], "xrp": "99.99"}

    try:
        carrier_balance = get_account_balance(state["carrier_wallet"]["address"])
    except Exception:
        carrier_balance = {"address": state["carrier_wallet"]["address"], "xrp": "100.00"}

    return {
        "channel":          record.to_dict(),
        "live_on_chain":    live,
        "cfo_balance":      cfo_balance,
        "carrier_balance":  carrier_balance,
        "agent_limits":     {k: str(v) for k, v in AGENT_LIMITS.items()},
        "daily_spend":      {k: str(v) for k, v in daily_spend_tracker.items()},
        "rlusd_issuer":     RLUSD_TESTNET_ISSUER,
        "network":          XRPL_TESTNET_RPC,
    }


# ---------------------------------------------------------------------------
# ENDPOINT — Top up the active payment channel
# ---------------------------------------------------------------------------
@app.post("/api/v1/channel-fund")
async def channel_fund(request: ChannelFundingRequest):
    """CFO adds XRP-backed capacity to the active payment channel."""
    record = _load_channel_record()
    if not record.is_open:
        raise HTTPException(status_code=409, detail="The active payment channel is closed.")

    if not CFO_WALLET_SEED:
        raise HTTPException(status_code=503, detail="CFO wallet seed is not configured.")
    cfo_wallet = Wallet.from_seed(CFO_WALLET_SEED)
    if cfo_wallet.address != record.cfo_address:
        raise HTTPException(status_code=403, detail="Configured CFO wallet does not own the active channel.")

    try:
        result = await asyncio.to_thread(
            fund_payment_channel,
            cfo_wallet=cfo_wallet,
            channel_record=record,
            top_up_rlusd=request.amount_rlusd,
        )
    except XRPLTreasuryError as exc:
        logger.error("Channel top-up failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _save_channel_record(record)
    timestamp = datetime.now(timezone.utc).isoformat()
    funding_event = {
        "log_id": str(uuid.uuid4()),
        "event_type": "CHANNEL_FUNDING",
        "timestamp": timestamp,
        "freight_id": "CHANNEL-TOP-UP",
        "truck_plate": "CFO Treasury",
        "carrier_wallet": record.carrier_address,
        "base_freight_rlusd": "0.00",
        "dwell_hours": 0,
        "free_hours": 0,
        "billable_hours": 0,
        "demurrage_rlusd": "0.00",
        "total_payout_rlusd": "0.00",
        "top_up_rlusd": str(request.amount_rlusd),
        "channel_id": record.channel_id,
        "channel_cap_rlusd": str(record.max_rlusd),
        "channel_remaining": str(record.remaining_rlusd),
        "decision": "FUNDED",
        "tx_hash": result["tx_hash"],
        "explorer_url": result["explorer_url"],
        "reasoning": f"CFO funded active Payment Channel with {request.amount_rlusd} RLUSD equivalent on XRPL Testnet.",
        "pod_parsed_fields": {},
    }
    _append_audit_log(funding_event)
    return {
        "status": "FUNDED",
        "channel": record.to_dict(),
        "funding_event": funding_event,
        "xrp_amount": result["xrp_amount"],
    }


# ---------------------------------------------------------------------------
# ENDPOINT — Update Agent Limits (CFO)
# ---------------------------------------------------------------------------
@app.post("/api/v1/update-limits")
async def update_limits(update: AgentLimitsUpdate):
    """CFO adjusts agent operating limits in real-time (no XRPL tx required)."""
    changed = {}
    if update.base_freight_usd is not None:
        AGENT_LIMITS["base_freight_usd"] = Decimal(str(update.base_freight_usd))
        changed["base_freight_usd"] = str(AGENT_LIMITS["base_freight_usd"])
    if update.free_hours is not None:
        AGENT_LIMITS["free_hours"] = update.free_hours
        changed["free_hours"] = update.free_hours
    if update.demurrage_rate_per_hour is not None:
        AGENT_LIMITS["demurrage_rate_per_hour"] = Decimal(str(update.demurrage_rate_per_hour))
        changed["demurrage_rate_per_hour"] = str(AGENT_LIMITS["demurrage_rate_per_hour"])
    if update.max_single_trip_rlusd is not None:
        AGENT_LIMITS["max_single_trip_rlusd"] = Decimal(str(update.max_single_trip_rlusd))
        changed["max_single_trip_rlusd"] = str(AGENT_LIMITS["max_single_trip_rlusd"])
    if update.max_daily_rlusd is not None:
        AGENT_LIMITS["max_daily_rlusd"] = Decimal(str(update.max_daily_rlusd))
        changed["max_daily_rlusd"] = str(AGENT_LIMITS["max_daily_rlusd"])
    if update.demurrage_rate_cap is not None:
        AGENT_LIMITS["demurrage_rate_cap"] = Decimal(str(update.demurrage_rate_cap))
        changed["demurrage_rate_cap"] = str(AGENT_LIMITS["demurrage_rate_cap"])
    if update.autonomous_mode is not None:
        AGENT_LIMITS["autonomous_mode"] = update.autonomous_mode
        changed["autonomous_mode"] = update.autonomous_mode

    logger.info("Agent limits updated: %s", changed)
    return {"status": "LIMITS_UPDATED", "changed": changed, "current": {k: str(v) for k, v in AGENT_LIMITS.items()}}


# ---------------------------------------------------------------------------
# ENDPOINT — Kill Switch
# ---------------------------------------------------------------------------
@app.post("/api/v1/kill-switch")
def kill_switch():
    """CFO-activated emergency closure of the active payment channel."""
    record = _load_channel_record()
    if not record.is_open:
        return {"status": "ALREADY_CLOSED", "channel_id": record.channel_id}

    cfo_wallet, _ = _load_wallets()
    result = emergency_close_channel(cfo_wallet=cfo_wallet, channel_record=record)
    _save_channel_record(record)

    logger.warning("KILL-SWITCH activated by operator. Channel %s closed.", record.channel_id)
    return result


# ---------------------------------------------------------------------------
# ENDPOINT — Health Check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "TransitPay AI Agent",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# STARTUP — load persisted audit logs
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def load_audit_logs():
    if AUDIT_LOG_FILE.exists():
        with open(AUDIT_LOG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    audit_logs.append(json.loads(line))
        logger.info("Loaded %d audit logs from disk.", len(audit_logs))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run("agent_service:app", host="0.0.0.0", port=port, reload=True)
