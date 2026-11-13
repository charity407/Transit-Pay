# TransitPay AI
## Autonomous Freight & Demurrage Settlement Agent
### Ripple Agentic Finance Hackathon

<div align="center">

```
  ______                      _ __  ____             ___    ____
 /_  __/________ _____  _____(_) /_/ __ \____ ___  _/   |  /  _/
  / / / ___/ __ `/ __ \/ ___/ / __/ /_/ / __ `/ / / / /| |  / /  
 / / / /  / /_/ / / / (__  ) / /_/ ____/ /_/ / /_/ / ___ |_/ /   
/_/ /_/   \__,_/_/ /_/____/_/\__/_/    \__,_/\__, /_/  |_/___/   
                                            /____/                 
```

**Freight settlement workflows on XRPL Testnet · Northern Corridor · Africa**

[![XRPL](https://img.shields.io/badge/XRPL-Testnet-blue?logo=xrp)](https://testnet.xrpl.org)
[![RLUSD](https://img.shields.io/badge/Stablecoin-RLUSD-green)](https://docs.ripple.com/products/stablecoin)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Frontend-Next.js%2014-black?logo=next.js)](https://nextjs.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

</div>

---

## 1. The African Market Problem
### The Northern Corridor Crisis

The **Mombasa–Nairobi–Kampala–Kigali corridor** connects the Port of Mombasa with
the Great Lakes region. The [Northern Corridor
Transit and Transport Coordination Authority (NCTTCA)](https://www.ttcanc.org/)
describes the corridor as serving six member states and a regional population of
more than 250 million ([NCTTCA, official website](https://www.ttcanc.org/)).
This is a corridor-scale population figure, not a measure of annual trade value.

The project addresses operational challenges that can arise in freight payments:
manual proof-of-delivery review, demurrage disputes, cross-border payment delays,
and working capital tied up while invoices are reconciled. The earlier market
loss and fee estimates have been removed because they did not have verifiable,
figure-specific sources. This README does not present them as measured industry
statistics.

**Illustrative workflow:** An FMCG manufacturer ships goods to Kampala via a
freight carrier. The truck arrives at a warehouse, is delayed during offloading
(demurrage), and delivers the goods. The carrier may then:

1. File a paper invoice with the demurrage claim
2. Wait for the shipper's finance team to audit it
3. Raise a dispute if amounts differ
4. Receive payment through the shipper's chosen payment channel

**Potential impact:** Delayed freight settlement can put pressure on carrier cash
flow and complicate reconciliation. The corridor-specific effects and magnitudes
vary by operator; no single regional rate or average is asserted here.

---

## 2. Why RLUSD + XRPL Solves This

### Stablecoin Settlement Design

Ripple describes RLUSD (Ripple USD) as a USD-backed stablecoin designed to be
redeemable 1:1 for US dollars ([Ripple RLUSD documentation](https://docs.ripple.com/products/stablecoin)).
This description is the issuer's statement; actual redemption and availability
depend on the applicable terms and service.

RLUSD denomination is intended to make invoice amounts easier to reconcile
against USD-denominated freight charges. The Testnet demo locks native XRP and
tracks an RLUSD-denominated proxy; it does not eliminate XRP price risk or
guarantee a USD value.

### XRPL Payment Channels: The Technical Approach

*XRPL Payment Channels lock native XRP liquidity mapped 1:1 as an RLUSD-denominated settlement proxy on Testnet. Native RLUSD IOU vault settlement using XLS-65 is a future direction, subject to Mainnet amendment availability and implementation.* This demo does not lock or transfer native RLUSD IOUs in its payment channel.

```
CFO pre-funds an XRP channel with the XRP equivalent of a $1,000 RLUSD-denominated cap
                ↓
XRPL Ledger locks native XRP; TransitPay tracks the mapped RLUSD proxy balance
                ↓  
Agent can ONLY claim within the mapped channel capacity (enforced by consensus nodes)
                ↓
If agent attempts to claim beyond capacity → XRPL rejects the claim
```

This is not a software permission check — the underlying XRP channel ceiling is enforced by
XRPL consensus. No CFO approval is required per transaction.

#### PaymentChannelClaim amount handling

XRPL channel claims use cumulative channel balances denominated in XRP drops, not the
RLUSD value of an individual trip. TransitPay converts the cumulative payout to the
channel's drop-denominated value, signs that same cumulative value, and supplies
consistent `Amount` and `Balance` fields in `PaymentChannelClaim`. This avoids
malformed amount errors such as `temBAD_AMOUNT`; the application still tracks payouts
and channel limits in RLUSD.

The configured agent key signs the channel-claim authorization. The XRPL channel
destination (the carrier) must submit and sign the outer `PaymentChannelClaim`
transaction, as required by the ledger; the CFO account remains the channel funder
and closer.

### Our Competitors
1. Bank and correspondent payment services: provide cross-border transfers; TransitPay's focus is tying a freight payout decision to delivery evidence and configured demurrage rules.
2. Freight platforms: support freight matching and fleet workflows; TransitPay's focus is integrating delivery verification with a settlement workflow.
3. Generic payment and stablecoin APIs: provide payment infrastructure; TransitPay's focus is adding freight-specific proof-of-delivery and demurrage checks.

These are high-level product categories, not a verified feature-by-feature
assessment of individual providers. Pricing, transfer times, and service
capabilities vary by provider, route, and account.

The demo's corridor presets, amounts, confidence labels, and timing examples are
configured or synthetic examples, not measured industry results.

Why CFOs Choose TransitPay AI: Unlike standard AI payment tools that require blind trust in software permissions, our XRPL Payment Channels enforce hard spending ceilings at the consensus layer (tecINSUFF_PAY_CHANNEL_FUNDS), paired with a step-by-step audit drawer and an on-chain kill-switch.

### Comparative Advantage

| Feature | SWIFT Wire | M-Pesa B2B | XRPL + RLUSD |
|---|---|---|---|
| Settlement time | Varies by provider and route | Varies by service | Depends on XRPL ledger/network conditions |
| Cross-border support | Depends on bank and corridor | Depends on service and market | XRPL network access; local requirements still apply |
| Currency stability | Depends on currency and instrument | KES-denominated services | XRP-backed RLUSD-denominated proxy in this Testnet demo |
| Programmable limits | No | No | **Yes (PayChannel)** |
| Autonomous agent-safe | No | No | **Yes (cryptographic)** |
| Fee (per transaction) | Varies | Varies | Depends on XRPL transaction and network conditions |
| Audit trail | Bank records | M-Pesa logs | **Immutable ledger** |

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          TransitPay AI System                            │
│                                                                           │
│   [ Driver / Carrier ]                                                    │
│         │  Upload POD Image + GPS Telemetry JSON                          │
│         ▼                                                                 │
│   ┌─────────────────────────────────────────────────────────┐            │
│   │           AI Agent Brain (Python / FastAPI)              │            │
│   │                                                           │            │
│   │  1. POD Vision Parser (GPT-4o Vision API)                │            │
│   │     - Extract: Arrival Time, Departure Time,             │            │
│   │       Freight ID, Carrier Wallet Address                  │            │
│   │     - Confidence score validation (≥ 0.80)               │            │
│   │                                                           │            │
│   │  2. GPS Geofence Dwell Analyzer                           │            │
│   │     - Confirm truck residency time at warehouse           │            │
│   │     - Calculate billable dwell: total - free_hours        │            │
│   │                                                           │            │
│   │  3. Demurrage Engine                                      │            │
│   │     - Total = Base Freight + (Billable hrs × Rate)        │            │
│   │     - Rate capped at CFO-configured maximum               │            │
│   │                                                           │            │
│   │  4. LLM Authorisation (chain-of-thought audit)            │            │
│   │     - Validates all 5 compliance rules                    │            │
│   │     - Flags anomalies (delivery_status ≠ COMPLETED, etc.) │            │
│   └─────────────────────────────────────────────────────────┘            │
│         │                                                                 │
│         ▼  Signed PaymentChannelClaim (agent-side, pre-flight guard)      │
│   ┌─────────────────────────────────────────────────────────┐            │
│   │         XRPL SDK Execution Layer (xrpl-py)               │            │
│   │                                                           │            │
│   │  - Safety Check 1: amount > 0                            │            │
│   │  - Safety Check 2: amount ≤ channel.remaining_rlusd      │            │
│   │  - Safety Check 3: on-chain channel balance verification  │            │
│   │  - Sign PaymentChannelClaim with agent keypair           │            │
│   └─────────────────────────────────────────────────────────┘            │
│         │                                                                 │
│         ▼  Broadcast to XRPL Testnet                                      │
│   ┌─────────────────────────────────────────────────────────┐            │
│   │   XRPL Testnet (wss://s.altnet.rippletest.net:51233)     │            │
│   │                                                           │            │
│   │  - XRPL consensus enforces payment-channel constraints    │            │
│   │  - tecINSUFF_PAY_CHANNEL_FUNDS rejects over-claims       │            │
│   │  - Confirmation time varies with ledger and network state  │            │
│   │  - Explorer: testnet.xrpl.org                             │            │
│   └─────────────────────────────────────────────────────────┘            │
│                              ▲                                            │
│   ┌─────────────────────────────────────────────────────────┐            │
│   │     CFO Control Dashboard (Next.js 14 + Tailwind)        │            │
│   │                                                           │            │
│   │  - Live treasury & channel balance                        │            │
│   │  - CFO Treasury Guardrails + Freight Rate Matrix sliders  │            │
│   │  - Inbound Delivery Simulator with Scanned POD thumbnail  │            │
│   │  - Searchable Settlement History + Testnet TX links       │            │
│   │  - OCR Inspector modal with GPS cross-audit panel         │            │
│   │  - Emergency kill-switch (PaymentChannelClose tx)         │            │
│   └─────────────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Key Features

### 1. Cryptographic Agent Governance
The AI agent **never has access to the CFO's main treasury wallet**. The CFO pre-funds an XRPL
Payment Channel with a strict per-channel cap. The agent can only issue `PaymentChannelClaim`
transactions — and the XRPL network's consensus mechanism enforces the ceiling. Even if the AI
agent were compromised or malicious, it cannot drain more than the channel cap.

### 2. Vision-Powered POD Verification
GPT-4o Vision parses Proof of Delivery receipt images, extracting structured fields (Freight ID,
Arrival/Departure timestamps, Carrier wallet) with a confidence score. Only PODs with confidence
≥ 80% are auto-approved; borderline documents route to human review.

### 3. Interactive Scanned Bill of Lading & OCR Inspector

The inbound delivery simulator includes a compact scanned-document preview beside the
freight identifier and a **View Scanned Bill of Lading** inspection action. Its
Northern Corridor demo document is styled as an East African consignment note for
routes such as Mombasa Port Gate 7 → Kampala Central Warehouse and Malaba Border.
It presents a consignment barcode, truck registration, cargo manifest, carrier
wallet, receiver acceptance, and physical gate stamps. Gate-In is shown as
**08:30:00 AM**; Gate-Out is calculated from the selected dwell duration.

The high-resolution inspection modal provides OCR overlay and laser-beam toggles,
plus a GPS-versus-physical-stamp cross-audit panel. The demo displays confidence
labels for the waybill (99.4%), arrival stamp (98.8%), departure stamp (99.1%),
carrier wallet (97.8%), and receiver signature (96.5%). Its standard corridor
scenario reports a 0.0-minute stamp/GPS discrepancy. These are demo inspection
values; actual POD extraction and compliance decisions are handled by the agent's
Vision/OCR flow.

Use **Upload Scanned POD** to select a document for the simulator, or drag an image
or PDF onto the open inspection modal. The selected file is read in the browser
and displayed in the inspector. OCR submission accepts PNG, JPEG, or WebP images
up to 5 MB; PDFs can be inspected in the UI but are not accepted by the current
OCR request API. The inspector's zoom controls adjust the document from 50% to
200%, with a reset to 100%. After processing, the scan is available in that
session's settlement dossier.

### 4. Verified Settlement History

The **Live Agent Telemetry & Settlement History** table lists delivery decisions,
POD scan actions, route and dwell details, settlement amount, and XRPL testnet
transaction hashes. Search filters immediately by freight ID, truck plate, or
transaction hash; the status tabs show all events, settled claims, or rejected
claims. Each row's **View Scan** action opens its POD dossier, and selecting a row
opens the delivery reasoning drawer with the OCR summary and settlement proof.

Approved transactions link to the XRPL Testnet Explorer, and the full transaction
hash can be copied from the feed or dossier. New results appear at the top of the
feed. The backend appends audit events—including approved and rejected decisions—
to `backend/audit_logs.jsonl` and loads them for the history API after restart.
Uploaded document bytes are associated with the current browser session's dossier;
the JSONL audit record stores the extracted fields and decision, not the original
image.

### 5. Top Up the Active Channel

The CFO dashboard provides **Top-Up Float** actions in the Active Channel Float
card and active-channel banner. Choose a custom RLUSD-equivalent amount or use the
$200, $500, and $1,000 presets, then confirm the XRPL deposit. The backend submits
a `PaymentChannelFund` transaction from the configured CFO wallet to the active
channel on XRPL Testnet. Successful top-ups increase the channel cap and available
float, update the CFO reserve display, and add a confirmed funding event—with its
transaction hash and explorer link—to the settlement history and
`backend/audit_logs.jsonl`. Top-ups are shown as funding events, not freight payouts.

### 6. Precise Demurrage Calculation
```python
billable_hours = max(0, dwell_hours - free_hours)        # 4 hrs (6 total - 2 free)
demurrage_charge = billable_hours * min(rate, rate_cap)  # 4 × $20 = $80
total_payout = base_freight + demurrage_charge            # $500 + $80 = $580 RLUSD
```

### 7. One-Click Kill-Switch
The CFO can revoke the active channel by clicking "Activate Kill-Switch" in the
dashboard. This submits a channel-close request on-ledger; closure and return of
remaining funds follow XRPL Payment Channel rules and timing. Restarting the agent
does not reopen a closed channel.

### 8. Full Immutable Audit Trail
Every agent decision — approved or rejected — is logged with:
- Chain-of-thought LLM reasoning
- Input POD fields + GPS telemetry
- XRPL transaction hash with explorer link
- Channel cap verification results

---

## 5. Phased Go-To-Market Strategy

The milestones, customer counts, pricing, and settlement volumes in this section
are TransitPay planning assumptions and targets—not external market statistics,
current performance, or customer commitments.

### Phase 1: Shadow Mode (Months 1–3)
**Target:** 3 FMCG manufacturers on the Northern Corridor (pilot: Unilever Kenya, Bidco Africa)

- Agent audits PODs and calculates demurrage, **recommends payouts** only
- CFO clicks manual "Execute" button in dashboard
- KPI: Measure dispute resolution against a pilot baseline; proposed target is 3 days
- Revenue model: \$50/month SaaS per logistics corridor managed

### Phase 2: Restricted Autonomy (Months 4–9)
**Target:** 20 freight carriers, 5 FMCG shippers

- Agent executes payouts autonomously up to **\$500 per-trip** Payment Channel limit
- Multi-channel support: one channel per carrier, weekly top-up by CFO
- M-Pesa off-ramp integration: RLUSD → KES settlement for small carriers without XRPL wallets
- KPI: Process 500+ deliveries/month, \$2M+ in RLUSD settled
- Revenue model: 0.3% of settled value + \$200/month per corporate account

### Phase 3: Enterprise Scale (Months 10–24)
**Target:** ERP integration, regional expansion to West Africa (Lagos-Abidjan corridor)

- SAP/Oracle ERP plugin: TransitPay AI becomes the settlement engine for freight POs
- Lori Systems integration: direct API for carrier fleet management
- Automated M-Pesa off-ramping at scale via Circle/Bitpesa RLUSD liquidity pools
- XLS-65 Single Asset Vaults: pool RLUSD across multiple corporate treasuries for shared liquidity
- KPI: \$50M+ annualised RLUSD settlement volume
- Revenue model: Enterprise SaaS (\$5,000/month) + 0.15% settlement fee

---

## 6. Quick Start & Installation Guide

### Prerequisites
- **Python 3.11 – 3.14** (tested and verified on Python 3.14)
- **Node.js 18+** & npm
- **XRPL Testnet Access** (public testnet RPC `https://s.altnet.rippletest.net:51234` — no sign-up required)
- **OpenAI API Key** (optional — built-in mock fallback handles OCR and compliance logic if key is omitted)

---

### Step 1: Backend Installation & Setup

1. **Navigate to the backend directory and create a virtual environment (recommended):**
   ```bash
   cd backend
   python -m venv venv

   # On Windows (PowerShell):
   .\venv\Scripts\Activate.ps1

   # On macOS / Linux:
   source venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   > *Note:* Dependencies use flexible version constraints (`fastapi>=0.111.0`, `xrpl-py>=2.6.0`, `pydantic>=2.7.1`) to ensure zero-build-error installations across Windows, Linux, and macOS.

3. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Your `.env` file should contain:
   ```ini
   # Optional: OpenAI API Key (leave empty for autonomous mock fallback)
   OPENAI_API_KEY=

   # XRPL Testnet Seeds (pre-funded live testnet wallets)
   CFO_WALLET_SEED=<your-cfo-testnet-wallet-seed>
   CARRIER_WALLET_SEED=<your-carrier-testnet-wallet-seed>
   AGENT_WALLET_SEED=<your-agent-testnet-wallet-seed>

   # RLUSD Testnet Issuer
   RLUSD_TESTNET_ISSUER=rQhWct2fv4Hhm3KVj3GhW8TQpnfQCMUDed

   # Service Port
   PORT=8001

   # API keys and allowed browser origin (replace the development keys outside local demos)
   CFO_API_KEY=tp_cfo_dev_key_2026
   AGENT_API_KEY=tp_agent_dev_key_2026
   FRONTEND_ORIGIN=http://localhost:3000
   ```

4. **Bootstrap the XRPL Testnet Channel:**
   `channel_state.json` is generated locally and contains public wallet/channel metadata only. Wallet seeds stay in the local `.env` file. After setting wallet seeds in `.env`, initialize the trustlines and payment channel:
   ```bash
   python xrpl_treasury.py
   ```
   This performs XRPL Testnet transactions and writes local channel state. Never commit `.env` or `channel_state.json`.

5. **Start the FastAPI Agent Backend:**
   ```bash
   python -m uvicorn agent_service:app --host 127.0.0.1 --port 8001 --reload
   ```
   - **Service Health Check:** `http://localhost:8001/health`
   - **Interactive Swagger Docs:** `http://localhost:8001/docs`

#### Restarting the Agent After the CFO Kill-Switch

The kill-switch closes the current XRPL Payment Channel on-ledger. Restarting
FastAPI alone does **not** reopen that channel, and a closed channel cannot be
funded again. The CFO must create a replacement channel; the previous channel's
remaining XRP stays subject to its XRPL settlement delay before it can be returned.

1. Stop the backend with **Ctrl+C**.
2. Check that `backend/.env` still contains the CFO, carrier, and agent wallet
   seeds for the intended wallets. The agent seed must match the key that the
   replacement channel will register. Never put seeds in chat, logs, or
   `channel_state.json`.
3. Confirm the CFO wallet has enough Testnet XRP to fund a new channel and pay
   transaction fees. The default bootstrap opens a new channel with a
   $1,000 RLUSD-denominated XRP proxy cap; funds still locked in the closed
   channel are not immediately available for this.
4. From the `backend` directory, run the bootstrap **once**:
   ```bash
   python xrpl_treasury.py
   ```
   This creates a new on-ledger channel and replaces the local
   `channel_state.json` with its public channel/wallet metadata. Do not rerun
   bootstrap just to restart the process: every successful run creates and funds
   another channel.
5. Start the backend again:
   ```bash
   python -m uvicorn agent_service:app --host 127.0.0.1 --port 8001 --reload
   ```
   Startup reconciles the replacement channel with XRPL.
6. Check `http://localhost:8001/health`, then
   `http://localhost:8001/api/v1/channel-status`. Confirm `channel.is_open` is
   `true` and verify the new `channel_id` before processing deliveries. Refresh
   the dashboard if it is already open.

---
~
### Step 2: Frontend Installation & Launch

1. **Navigate to the frontend directory:**
   ```bash
   cd frontend
   ```

2. **Install Node dependencies:**
   ```bash
   npm install
   ```

3. **Configure `.env.local` for backend port 8001 and the local CFO API key:**
   ```ini
   NEXT_PUBLIC_AGENT_API_URL=http://127.0.0.1:8001
   NEXT_PUBLIC_CFO_API_KEY=tp_cfo_dev_key_2026
   ```
   The browser key is included in client-side code and is suitable only for local demos; it is
   not a production secret. Use a server-side proxy and production authentication in deployment.

4. **Launch the Next.js 14 Dashboard:**
   ```bash
   npm run dev
   ```
   Open **[http://localhost:3000](http://localhost:3000)** in your browser.

#### Troubleshooting: Next.js `Cannot find module './<chunk>.js'`

The Next.js development server and production build share the generated `frontend/.next`
directory. Stop any running Next.js server before running `npm run build`. If a dev
server reports a missing webpack chunk (for example `Cannot find module './682.js'`),
stop the dev server, remove the generated cache, and restart it:

```powershell
Set-Location frontend
Remove-Item -Recurse -Force .next
npm run dev
```

The `.next` directory is generated and will be recreated when Next.js starts.

---

### Step 3: Run the Automated Test Suite

Run the full pytest suite (28 tests covering demurrage arithmetic, POD extraction and upload validation, deterministic policy/auth/idempotency controls, XRPL channel governance and reconciliation, channel funding, and delivery processing):

```bash
cd ../backend
python -m pytest tests/test_transitpay.py -v
```

---

### Step 4: Triggering Payouts

You can trigger settlements in two ways:

#### Option A: Direct from the CFO Dashboard UI (Recommended for Judges)
1. Open **[http://localhost:3000](http://localhost:3000)**.
2. Locate the **Inbound Delivery Simulator** card above the Telemetry Feed.
3. *(Optional)* Click **Upload Scanned POD** to attach your own Bill of Lading image, or click **View Scanned Bill of Lading** (the thumbnail preview beside the freight identifier) to open the full **OCR Inspector Modal** — which displays animated OCR bounding boxes, physical ink stamp positions, and a GPS cross-audit panel.
4. Select any African corridor preset:
   - **Standard Freight (Mombasa → Kampala)** (3.5 h dwell = \$530.00 RLUSD)
   - **Border Bottleneck (Malaba Customs)** (6.0 h dwell = \$580.00 RLUSD)
   - **Severe Depot Congestion (Kampala)** (11.0 h dwell = \$680.00 RLUSD)
   - **Over-Cap Breach Attempt** (35.0 h dwell = Blocked by Ledger Limit)
5. Click **`Process Inbound Delivery`**.
6. The agent immediately audits the claim, submits a `PaymentChannelClaim` to XRPL Testnet, and slides out the **AI Reasoning Dossier** with the live transaction hash.
7. The new entry appears instantly at the top of the **Live Agent Telemetry & Settlement History** table. Click **View Scan** on the row to inspect the POD dossier, or click the 64-character TX hash to open it on [testnet.xrpl.org](https://testnet.xrpl.org). Use the **search bar** or **All / Settled / Rejected** filter tabs to explore past events.

#### Option B: Via Terminal / API

**Using Python:**
```bash
python -c "
import urllib.request, json
payload = {
    'freight_id': 'FRT-2024-KLA-0099',
    'truck_plate': 'KDA-419J',
    'geofence_name': 'Mombasa Port Gate 7 to Kampala Central',
    'entry_timestamp': '2024-07-15T08:30:00Z',
    'exit_timestamp': '2024-07-15T12:00:00Z',
    'total_dwell_minutes': 210,
    'geofence_lat': 0.3476,
    'geofence_lon': 32.5825,
    'carrier_wallet': 'rLytz9wbRUkJnBDL21SmSSvDkMY6J4rXRp'
}
req = urllib.request.Request('http://localhost:8001/api/v1/process-delivery', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'X-API-Key': 'tp_cfo_dev_key_2026'})
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
"
```

**Using cURL:**
```bash
curl -X POST http://localhost:8001/api/v1/process-delivery \
  -H "Content-Type: application/json" \
  -H "X-API-Key: tp_cfo_dev_key_2026" \
  -d '{
    "freight_id": "FRT-2024-KLA-0042",
    "truck_plate": "KAA-123K",
    "geofence_name": "Kampala Central Warehouse",
    "entry_timestamp": "2024-07-15T08:30:00Z",
    "exit_timestamp": "2024-07-15T16:30:00Z",
    "total_dwell_minutes": 360,
    "geofence_lat": 0.3476,
    "geofence_lon": 32.5825,
    "carrier_wallet": "rLytz9wbRUkJnBDL21SmSSvDkMY6J4rXRp"
  }'
```

**Illustrative JSON response:** Values, including the transaction identifier and
channel balances, are sample data and do not represent a live or current payment.
```json
{
  "log_id": "e89dbea1-ac58-4713-8c66-24603b031b19",
  "timestamp": "2026-11-06T17:05:34.494631+00:00",
  "freight_id": "FRT-2024-KLA-0099",
  "truck_plate": "KDA-419J",
  "carrier_wallet": "rLytz9wbRUkJnBDL21SmSSvDkMY6J4rXRp",
  "base_freight_rlusd": "500.00",
  "dwell_hours": 3.5,
  "free_hours": 2,
  "billable_hours": 1.5,
  "demurrage_rlusd": "30.00",
  "total_payout_rlusd": "530.00",
  "kes_equivalent": "~68,370 KES",
  "channel_id": "FA22F3FDDCF052352F772596302F27B789E5CE3720D631444ACB2B62A72F02A6",
  "channel_cap_rlusd": "1000.00",
  "channel_remaining": "470.00",
  "decision": "APPROVED",
  "tx_hash": "61CD082B878BEE14998637BD94482BC98D545DB53396A5D3D94C0D280E83278A",
  "explorer_url": "https://testnet.xrpl.org/transactions/61CD082B878BEE14998637BD94482BC98D545DB53396A5D3D94C0D280E83278A",
  "reasoning": "Dwell time: 3.50 hrs. Free allowance: 2 hrs. Billable: 1.50 hrs × $20.0/hr = $30.0 demurrage. Base freight: $500.0. Total payout: $530.0 RLUSD. Within channel cap. Approved for XRPL settlement."
}
```

---

## 7. Project Structure

```
TransitPay/
├── backend/
│   ├── xrpl_treasury.py        # Section 1: XRPL Payment Channel + RLUSD setup
│   ├── agent_service.py        # Section 2: Autonomous AI agent brain (FastAPI)
│   ├── channel_state.json      # Persisted channel state (auto-generated)
│   ├── audit_logs.jsonl        # Append-only audit log
│   ├── requirements.txt
│   ├── .env.example
│   └── tests/
│       └── test_transitpay.py  # Pytest test suite
│
├── frontend/
│   ├── app/
│   │   ├── page.tsx            # Section 3: CFO Control Dashboard
│   │   ├── layout.tsx
│   │   └── globals.css
│   ├── .env.local
│   └── package.json
│
├── LICENSE                     # MIT License
└── README.md                   # Project guide, feature summary, and references
```

---

## 8. XRPL Technical References

### Sources and attribution

- **Corridor geography and population:** The more-than-250-million regional
  population figure is attributed to the [Northern Corridor Transit and Transport
  Coordination Authority (NCTTCA)](https://www.ttcanc.org/), which describes the
  corridor's member states and regional population (official website, accessed
  2026-11-13). No annual corridor trade-value estimate is claimed here.
- **RLUSD backing and redemption description:** [Ripple's RLUSD documentation](https://docs.ripple.com/products/stablecoin)
  is the issuer source for the stated 1:1 USD backing/redemption design.
- **Payment-channel behavior:** XRPL protocol descriptions are linked below.

The README does not claim externally measured regional demurrage-loss, dispute,
default, banking-fee, or payment-delay averages. Roadmap KPIs and pricing are
TransitPay planning assumptions; dashboard OCR confidence values and example
settlement amounts are synthetic demo data.

| Resource | URL |
|---|---|
| NCTTCA official website | https://www.ttcanc.org/ |
| XRPL Payment Channels | https://xrpl.org/docs/concepts/payment-types/payment-channels |
| XRPL Agent Docs | https://xrpl.org/docs/agents |
| XRPL Agent Wallet Skill | https://xrpl.org/docs/agents/xrpl-agent-wallet-skill |
| RLUSD Documentation | https://docs.ripple.com/products/stablecoin |
| RLUSD Testnet Faucet | https://tryrlusd.com |
| XRPL Testnet Explorer | https://testnet.xrpl.org |
| xrpl-py SDK | https://xrpl-py.readthedocs.io |
| XLS-65 Single Asset Vaults | https://xrpl.org/docs/concepts/tokens/single-asset-vaults |
| Privy Agent Wallets | https://docs.privy.io |

---

## 9. Team & Acknowledgements

Built for the **Ripple Agentic Finance Hackathon**

> *"Africa's trade infrastructure is too important to run on 1980s payment rails.  
> XRPL + RLUSD + AI agents make instant, transparent, auditable freight settlement possible — today."*

---

<div align="center">

**TransitPay AI** · Moving Africa's Goods · Settling Africa's Payments · On-Chain · Instantly

</div>
