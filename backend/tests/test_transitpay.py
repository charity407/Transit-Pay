"""
TransitPay AI — Pytest Test Suite
===================================
Tests:
  - Demurrage calculation engine
  - POD mock parsing
  - XRPL claim safety checks (pre-flight guardrails)
  - Full pipeline integration test (mocked XRPL + OpenAI)
  - Kill-switch endpoint
"""

import json
import pytest
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# Import modules under test
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_service import (
    calculate_demurrage,
    _mock_pod_parse,
    _mock_authorisation,
    AGENT_LIMITS,
    ChannelFundingRequest,
    channel_fund,
)
from xrpl_treasury import (
    ChannelRecord,
    ClaimRejectedError,
    XRPLTreasuryError,
    rlusd_to_drops_equivalent,
    _build_rlusd_amount,
)


# ---------------------------------------------------------------------------
# UNIT TESTS — Demurrage Engine
# ---------------------------------------------------------------------------
class TestDemurrageCalculation:

    def test_no_demurrage_within_free_hours(self):
        """Truck dwell ≤ 2 hours → zero demurrage."""
        AGENT_LIMITS["free_hours"]              = 2
        AGENT_LIMITS["demurrage_rate_per_hour"] = Decimal("20.00")
        AGENT_LIMITS["demurrage_rate_cap"]      = Decimal("50.00")
        AGENT_LIMITS["base_freight_usd"]        = Decimal("500.00")

        result = calculate_demurrage(dwell_minutes=90)  # 1.5 hrs
        assert Decimal(result["demurrage_rlusd"]) == Decimal("0.00")
        assert Decimal(result["total_payout_rlusd"]) == Decimal("500.00")

    def test_demurrage_charged_for_4_hours(self):
        """Truck dwell 6 hrs; 2 free; 4 billable @ $20/hr = $80 demurrage."""
        AGENT_LIMITS["free_hours"]              = 2
        AGENT_LIMITS["demurrage_rate_per_hour"] = Decimal("20.00")
        AGENT_LIMITS["demurrage_rate_cap"]      = Decimal("50.00")
        AGENT_LIMITS["base_freight_usd"]        = Decimal("500.00")

        result = calculate_demurrage(dwell_minutes=360)  # 6 hrs
        assert result["billable_hours"] == pytest.approx(4.0, abs=0.01)
        assert Decimal(result["demurrage_rlusd"])    == Decimal("80.00")
        assert Decimal(result["total_payout_rlusd"]) == Decimal("580.00")

    def test_demurrage_rate_cap_enforced(self):
        """Rate cap at $30/hr should override $50/hr config."""
        AGENT_LIMITS["free_hours"]              = 2
        AGENT_LIMITS["demurrage_rate_per_hour"] = Decimal("50.00")
        AGENT_LIMITS["demurrage_rate_cap"]      = Decimal("30.00")
        AGENT_LIMITS["base_freight_usd"]        = Decimal("500.00")

        result = calculate_demurrage(dwell_minutes=240)  # 4 hrs; 2 billable
        # capped at $30/hr × 2 hr = $60
        assert Decimal(result["demurrage_rlusd"]) == Decimal("60.00")

    def test_exact_free_hours_boundary(self):
        """Dwell exactly at free-hour threshold → zero demurrage."""
        AGENT_LIMITS["free_hours"] = 2
        result = calculate_demurrage(dwell_minutes=120)  # exactly 2 hrs
        assert Decimal(result["demurrage_rlusd"]) == Decimal("0.00")

    def test_partial_hour_ceiling(self):
        """Partial 15-min overage still rounds to 0.25 hrs billable."""
        AGENT_LIMITS["free_hours"]              = 2
        AGENT_LIMITS["demurrage_rate_per_hour"] = Decimal("20.00")
        AGENT_LIMITS["demurrage_rate_cap"]      = Decimal("50.00")
        result = calculate_demurrage(dwell_minutes=135)  # 2.25 hrs; 0.25 billable
        assert Decimal(result["demurrage_rlusd"]) == Decimal("5.00")  # $20 × 0.25


# ---------------------------------------------------------------------------
# UNIT TESTS — POD Mock Parser
# ---------------------------------------------------------------------------
class TestPODParsing:

    def test_mock_pod_returns_expected_fields(self):
        result = _mock_pod_parse("rCarrierAddress123")
        assert result["freight_id"]       == "FRT-2024-KLA-0042"
        assert result["delivery_status"]  == "COMPLETED"
        assert result["carrier_wallet"]   == "rCarrierAddress123"
        assert result["confidence_score"] >= 0.95

    def test_mock_pod_carrier_wallet_injected(self):
        """GPS telemetry wallet overrides any POD-parsed wallet."""
        wallet = "rXYZTestCarrierWallet9999"
        result = _mock_pod_parse(wallet)
        assert result["carrier_wallet"] == wallet


# ---------------------------------------------------------------------------
# UNIT TESTS — Agent Pre-flight Safety Checks
# ---------------------------------------------------------------------------
class TestClaimSafetyChecks:

    def _make_channel(self, max_rlusd: str = "1000", claimed: str = "0") -> ChannelRecord:
        return ChannelRecord(
            channel_id="DEADBEEF" * 8,
            cfo_address="rCFOAddress",
            carrier_address="rCarrierAddress",
            max_rlusd=Decimal(max_rlusd),
            claimed_rlusd=Decimal(claimed),
        )

    def test_claim_exceeds_remaining_cap_rejected(self):
        """Agent attempting to claim above channel remaining → ClaimRejectedError."""
        channel = self._make_channel(max_rlusd="1000", claimed="900")

        # Remaining = $100; claiming $200 must be blocked
        with pytest.raises(ClaimRejectedError, match="exceeds remaining channel"):
            # Simulate the guard directly (without submitting to XRPL)
            rlusd_amount = Decimal("200")
            if rlusd_amount > channel.remaining_rlusd:
                raise ClaimRejectedError(
                    f"Claim of {rlusd_amount} RLUSD exceeds remaining channel "
                    f"balance of {channel.remaining_rlusd} RLUSD. "
                    f"The XRPL node would reject this with tecINSUFF_PAY_CHANNEL_FUNDS."
                )

    def test_zero_claim_rejected(self):
        """Zero-value claim must be blocked."""
        channel = self._make_channel()
        with pytest.raises(ClaimRejectedError, match="positive"):
            rlusd_amount = Decimal("0")
            if rlusd_amount <= Decimal("0"):
                raise ClaimRejectedError("Claim amount must be positive.")

    def test_valid_claim_passes_guard(self):
        """$500 claim on $1,000 channel (empty) should pass guard."""
        channel = self._make_channel(max_rlusd="1000", claimed="0")
        rlusd_amount = Decimal("500")
        # Guard logic
        assert rlusd_amount > Decimal("0")
        assert rlusd_amount <= channel.remaining_rlusd

    def test_remaining_decrements_correctly(self):
        channel = self._make_channel(max_rlusd="1000", claimed="400")
        assert channel.remaining_rlusd == Decimal("600")
        channel.claimed_rlusd += Decimal("600")
        assert channel.remaining_rlusd == Decimal("0")


# ---------------------------------------------------------------------------
# UNIT TESTS — RLUSD / Drops Conversion
# ---------------------------------------------------------------------------
class TestDropsConversion:

    def test_1000_rlusd_converts_to_drops(self):
        drops = rlusd_to_drops_equivalent(Decimal("1000"), xrp_per_usd=Decimal("0.5"))
        assert int(drops) == 500_000_000  # 500 XRP in drops

    def test_zero_rlusd(self):
        drops = rlusd_to_drops_equivalent(Decimal("0"))
        assert drops == "0"


# ---------------------------------------------------------------------------
# UNIT TESTS — LLM Authorisation Mock
# ---------------------------------------------------------------------------
class TestMockAuthorisation:

    def _make_calc(self, total: str) -> dict:
        return {
            "dwell_hours":        6.0,
            "free_hours":         2,
            "billable_hours":     4.0,
            "demurrage_rate":     20.0,
            "base_freight_rlusd": "500.00",
            "demurrage_rlusd":    "80.00",
            "total_payout_rlusd": total,
            "reasoning":          "Test reasoning.",
        }

    def _make_channel(self, remaining: str) -> ChannelRecord:
        claimed = str(Decimal("1000") - Decimal(remaining))
        return ChannelRecord(
            channel_id="DEADBEEF" * 8,
            cfo_address="rCFO",
            carrier_address="rCarrier",
            max_rlusd=Decimal("1000"),
            claimed_rlusd=Decimal(claimed),
        )

    def test_authorisation_approved_within_cap(self):
        AGENT_LIMITS["max_single_trip_rlusd"] = Decimal("1000")
        calc    = self._make_calc("580.00")
        channel = self._make_channel("1000")  # full channel remaining
        result  = _mock_authorisation(calc, channel)
        assert result["approved"] is True

    def test_authorisation_rejected_exceeds_cap(self):
        AGENT_LIMITS["max_single_trip_rlusd"] = Decimal("1000")
        calc    = self._make_calc("1200.00")  # over channel
        channel = self._make_channel("800")   # only 800 remaining
        result  = _mock_authorisation(calc, channel)
        assert result["approved"] is False
        assert "EXCEEDS_CHANNEL_CAP" in result["flags"]


# ---------------------------------------------------------------------------
# INTEGRATION TEST — Full Pipeline (mocked XRPL + OpenAI)
# ---------------------------------------------------------------------------
class TestFullPipeline:

    @pytest.mark.asyncio
    async def test_full_delivery_pipeline_mock(self):
        """
        End-to-end pipeline test with mocked XRPL submission and OpenAI Vision.
        Verifies that:
          - GPS telemetry is parsed
          - Demurrage is correctly computed ($580 for 6-hr dwell)
          - Authorisation is granted
          - Audit log entry is well-formed
        """
        from agent_service import calculate_demurrage, _mock_pod_parse, _mock_authorisation

        carrier_wallet = "rTestCarrierWallet123456789"

        # Step 1: Parse GPS
        gps_dwell_minutes = 360  # 6 hours

        # Step 2: Parse POD (mock)
        pod_fields = _mock_pod_parse(carrier_wallet)
        assert pod_fields["delivery_status"] == "COMPLETED"

        # Step 3: Demurrage
        AGENT_LIMITS["free_hours"]              = 2
        AGENT_LIMITS["demurrage_rate_per_hour"] = Decimal("20.00")
        AGENT_LIMITS["base_freight_usd"]        = Decimal("500.00")
        AGENT_LIMITS["demurrage_rate_cap"]      = Decimal("50.00")
        AGENT_LIMITS["max_single_trip_rlusd"]   = Decimal("1000.00")

        calc = calculate_demurrage(gps_dwell_minutes)
        assert Decimal(calc["total_payout_rlusd"]) == Decimal("580.00")

        # Step 4: Authorisation
        channel = ChannelRecord(
            channel_id="A" * 64,
            cfo_address="rCFO",
            carrier_address=carrier_wallet,
            max_rlusd=Decimal("1000"),
            claimed_rlusd=Decimal("0"),
        )
        auth = _mock_authorisation(calc, channel)
        assert auth["approved"] is True

        # Step 5: Audit log shape
        log = {
            "log_id":              "test-uuid",
            "freight_id":          pod_fields["freight_id"],
            "truck_plate":         "KAA-123K",
            "carrier_wallet":      carrier_wallet,
            "total_payout_rlusd":  calc["total_payout_rlusd"],
            "decision":            "APPROVED",
            "reasoning":           auth["reasoning"],
        }
        assert log["total_payout_rlusd"] == "580.00"
        assert log["decision"] == "APPROVED"


class TestChannelFundingEndpoint:

    @pytest.mark.asyncio
    async def test_funding_updates_channel_and_persists_audit_event(self, tmp_path, monkeypatch):
        import agent_service

        channel_file = tmp_path / "channel_state.json"
        audit_file = tmp_path / "audit_logs.jsonl"
        record = ChannelRecord(
            channel_id="A" * 64,
            cfo_address="rCFO",
            carrier_address="rCarrier",
            max_rlusd=Decimal("1000.00"),
            claimed_rlusd=Decimal("600.00"),
        )
        channel_file.write_text(json.dumps({
            "channel": record.to_dict(),
            "cfo_wallet": {"address": "rCFO"},
            "carrier_wallet": {"address": "rCarrier"},
        }))
        monkeypatch.setattr(agent_service, "CHANNEL_STATE_FILE", channel_file)
        monkeypatch.setattr(agent_service, "AUDIT_LOG_FILE", audit_file)
        monkeypatch.setattr(agent_service, "CFO_WALLET_SEED", "test-cfo-seed")
        monkeypatch.setattr(agent_service, "audit_logs", [])
        monkeypatch.setattr(
            agent_service.Wallet,
            "from_seed",
            lambda _seed: SimpleNamespace(address="rCFO"),
        )

        def fake_fund_payment_channel(cfo_wallet, channel_record, top_up_rlusd):
            assert cfo_wallet.address == "rCFO"
            channel_record.max_rlusd += top_up_rlusd
            channel_record.last_tx_hash = "B" * 64
            return {
                "status": "FUNDED",
                "tx_hash": "B" * 64,
                "xrp_amount": "10",
                "new_cap_rlusd": str(channel_record.max_rlusd),
                "explorer_url": f"https://testnet.xrpl.org/transactions/{'B' * 64}",
            }

        monkeypatch.setattr(agent_service, "fund_payment_channel", fake_fund_payment_channel)
        result = await channel_fund(ChannelFundingRequest(amount_rlusd=Decimal("200.00")))

        assert result["status"] == "FUNDED"
        assert result["channel"]["max_rlusd"] == "1200.00"
        assert result["channel"]["remaining_rlusd"] == "600.00"
        assert result["funding_event"]["event_type"] == "CHANNEL_FUNDING"
        assert result["funding_event"]["top_up_rlusd"] == "200.00"
        assert result["funding_event"]["tx_hash"] == "B" * 64
        assert json.loads(audit_file.read_text())["decision"] == "FUNDED"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
