"""Unit tests for core data model types.

Tests construct each type and verify field access in isolation,
with no dependency on other components.
"""

import pytest
from dataclasses import FrozenInstanceError
from datetime import datetime
from eais_scheduling_agent.core.models import (
    BookingRequest,
    Decision,
    AuditRecord,
)


class TestBookingRequest:
    """Unit tests for BookingRequest dataclass."""

    def test_booking_request_construction(self):
        """Construct a BookingRequest and access all fields."""
        booking = BookingRequest(
            sector="clinic",
            fields={"practitioner": "Dr. Smith", "patient_name": "John Doe"},
            raw_text="I need an appointment with Dr. Smith for John Doe",
        )

        assert booking.sector == "clinic"
        assert booking.fields == {"practitioner": "Dr. Smith", "patient_name": "John Doe"}
        assert booking.raw_text == "I need an appointment with Dr. Smith for John Doe"

    def test_booking_request_with_empty_fields(self):
        """BookingRequest with empty fields dict."""
        booking = BookingRequest(
            sector="restaurant",
            fields={},
            raw_text="Make a reservation",
        )

        assert booking.sector == "restaurant"
        assert booking.fields == {}
        assert booking.raw_text == "Make a reservation"

    def test_booking_request_immutable(self):
        """BookingRequest is frozen (immutable)."""
        booking = BookingRequest(
            sector="clinic",
            fields={"patient": "Alice"},
            raw_text="test",
        )

        with pytest.raises(FrozenInstanceError):
            booking.sector = "restaurant"

    def test_booking_request_various_sectors(self):
        """BookingRequest works with various sector values."""
        sectors = ["clinic", "restaurant", "hotel", "retail"]
        for sector in sectors:
            booking = BookingRequest(
                sector=sector,
                fields={},
                raw_text="test",
            )
            assert booking.sector == sector


class TestDecision:
    """Unit tests for Decision dataclass."""

    def test_decision_confirmed_no_reason(self):
        """Decision with CONFIRMED status and no reason."""
        decision = Decision(status="CONFIRMED")

        assert decision.status == "CONFIRMED"
        assert decision.reason is None

    def test_decision_confirmed_with_none_reason(self):
        """Decision with CONFIRMED status and explicit None reason."""
        decision = Decision(status="CONFIRMED", reason=None)

        assert decision.status == "CONFIRMED"
        assert decision.reason is None

    def test_decision_pending_with_reason(self):
        """Decision with PENDING_APPROVAL status and a reason."""
        decision = Decision(
            status="PENDING_APPROVAL",
            reason="Patient age exceeds 65; requires additional review",
        )

        assert decision.status == "PENDING_APPROVAL"
        assert decision.reason == "Patient age exceeds 65; requires additional review"

    def test_decision_invalid_status(self):
        """Decision with invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Decision.status must be one of"):
            Decision(status="REJECTED")

    def test_decision_invalid_status_other_values(self):
        """Decision rejects various invalid status values."""
        invalid_statuses = ["rejected", "approved", "pending", "UNKNOWN", ""]
        for invalid_status in invalid_statuses:
            with pytest.raises(ValueError):
                Decision(status=invalid_status)

    def test_decision_immutable(self):
        """Decision is frozen (immutable)."""
        decision = Decision(status="CONFIRMED")

        with pytest.raises(FrozenInstanceError):
            decision.status = "PENDING_APPROVAL"

    def test_decision_case_sensitive_status(self):
        """Decision status values are case-sensitive."""
        # These should fail because they're lowercase
        with pytest.raises(ValueError):
            Decision(status="confirmed")
        with pytest.raises(ValueError):
            Decision(status="pending_approval")


class TestAuditRecord:
    """Unit tests for AuditRecord dataclass."""

    def test_audit_record_construction(self):
        """Construct an AuditRecord and access all fields."""
        timestamp = datetime(2026, 8, 4, 12, 30, 45)
        record = AuditRecord(
            input="Patient appointment request",
            skill_pack="clinic_scheduling",
            intent={"action": "book", "entity": "appointment"},
            rules_evaluated=["age_check", "availability_check"],
            decision="CONFIRMED",
            approval_status="approved",
            timestamp=timestamp,
        )

        assert record.input == "Patient appointment request"
        assert record.skill_pack == "clinic_scheduling"
        assert record.intent == {"action": "book", "entity": "appointment"}
        assert record.rules_evaluated == ["age_check", "availability_check"]
        assert record.decision == "CONFIRMED"
        assert record.approval_status == "approved"
        assert record.timestamp == timestamp

    def test_audit_record_with_empty_collections(self):
        """AuditRecord with empty intent dict and rules_evaluated list."""
        timestamp = datetime.now()
        record = AuditRecord(
            input="test",
            skill_pack="test_pack",
            intent={},
            rules_evaluated=[],
            decision="PENDING_APPROVAL",
            approval_status="pending",
            timestamp=timestamp,
        )

        assert record.intent == {}
        assert record.rules_evaluated == []

    def test_audit_record_immutable(self):
        """AuditRecord is frozen (immutable)."""
        record = AuditRecord(
            input="test",
            skill_pack="test",
            intent={},
            rules_evaluated=[],
            decision="CONFIRMED",
            approval_status="approved",
            timestamp=datetime.now(),
        )

        with pytest.raises(FrozenInstanceError):
            record.input = "modified"

    def test_audit_record_with_various_decision_values(self):
        """AuditRecord accepts various decision values."""
        timestamp = datetime.now()
        decisions = ["CONFIRMED", "PENDING_APPROVAL"]

        for decision_val in decisions:
            record = AuditRecord(
                input="test",
                skill_pack="test",
                intent={},
                rules_evaluated=[],
                decision=decision_val,
                approval_status="pending",
                timestamp=timestamp,
            )
            assert record.decision == decision_val

    def test_audit_record_with_complex_intent(self):
        """AuditRecord handles complex nested intent dict."""
        timestamp = datetime.now()
        complex_intent = {
            "action": "book",
            "entity": {"type": "appointment", "duration": 60},
            "constraints": ["morning", "requires_follow_up"],
        }
        record = AuditRecord(
            input="test",
            skill_pack="test",
            intent=complex_intent,
            rules_evaluated=["rule1", "rule2", "rule3"],
            decision="CONFIRMED",
            approval_status="approved",
            timestamp=timestamp,
        )

        assert record.intent == complex_intent
        assert len(record.rules_evaluated) == 3


class TestDataTypeIntegration:
    """Integration tests using multiple types together."""

    def test_booking_request_and_decision(self):
        """Use BookingRequest and Decision together."""
        booking = BookingRequest(
            sector="clinic",
            fields={"patient": "John"},
            raw_text="Book appointment",
        )
        decision = Decision(status="CONFIRMED", reason=None)

        assert booking.sector == "clinic"
        assert decision.status == "CONFIRMED"

    def test_decision_and_audit_record(self):
        """Use Decision and AuditRecord together."""
        decision = Decision(
            status="PENDING_APPROVAL",
            reason="Age check failed",
        )
        record = AuditRecord(
            input="test",
            skill_pack="clinic",
            intent={},
            rules_evaluated=["age_check"],
            decision=decision.status,
            approval_status="pending",
            timestamp=datetime.now(),
        )

        assert record.decision == decision.status
        assert "age_check" in record.rules_evaluated

    def test_full_booking_flow_data_types(self):
        """Test all three types in a typical booking flow."""
        # Incoming booking request
        booking = BookingRequest(
            sector="restaurant",
            fields={"party_size": 4, "datetime": "2026-08-05 18:30"},
            raw_text="Table for 4 tonight at 6:30 PM",
        )

        # Decision made
        decision = Decision(status="CONFIRMED", reason=None)

        # Audit record created
        audit = AuditRecord(
            input=booking.raw_text,
            skill_pack="restaurant_seating",
            intent=booking.fields,
            rules_evaluated=["capacity", "availability"],
            decision=decision.status,
            approval_status="approved",
            timestamp=datetime.now(),
        )

        # Verify all data flows through correctly
        assert booking.sector == "restaurant"
        assert audit.input == booking.raw_text
        assert audit.intent == booking.fields
        assert audit.decision == decision.status
