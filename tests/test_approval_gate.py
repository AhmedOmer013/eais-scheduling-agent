"""Tests for `StandardApprovalGate` (T7) -- unit-tested in isolation.

`RuleContext` and `BookingRequest` are constructed directly here; there is
no need to go through `SchedulingAgentCore` or a real skill pack to
exercise the gate's decision logic. Violation strings that mimic a real
skill pack's style (out-of-hours) or a not-yet-built one (over-capacity,
T10) are written inline -- the point is testing the gate's generic
handling of *any* violation string, not any particular sector's rules.
"""

from eais_scheduling_agent.core.gate import StandardApprovalGate
from eais_scheduling_agent.core.interfaces import RuleContext
from eais_scheduling_agent.core.models import BookingRequest, Decision

REQUEST = BookingRequest(sector="irrelevant", fields={}, raw_text="book something")


def build_gate() -> StandardApprovalGate:
    return StandardApprovalGate()


class TestCleanRequestIsConfirmed:
    def test_empty_rules_and_no_conflict_confirms(self):
        gate = build_gate()

        decision = gate.evaluate(REQUEST, RuleContext(), conflict=False)

        assert decision == Decision(status="CONFIRMED")
        assert decision.reason is None


class TestMissingFields:
    def test_missing_fields_escalate_with_field_names_in_reason(self):
        gate = build_gate()
        rules = RuleContext(missing_fields=("practitioner", "start_time"))

        decision = gate.evaluate(REQUEST, rules, conflict=False)

        assert decision.status == "PENDING_APPROVAL"
        assert "practitioner" in decision.reason
        assert "start_time" in decision.reason


class TestOutOfHoursViolation:
    def test_out_of_hours_violation_escalates_with_violation_text(self):
        gate = build_gate()
        # Same style ClinicSkillPack.validate() produces.
        violation = (
            "outside working hours: requested 20:00, hours are 09:00-17:00"
        )
        rules = RuleContext(violations=(violation,))

        decision = gate.evaluate(REQUEST, rules, conflict=False)

        assert decision.status == "PENDING_APPROVAL"
        assert violation in decision.reason


class TestOverCapacityViolation:
    def test_over_capacity_style_violation_escalates(self):
        """Generic handling of any violation string -- not a real restaurant pack."""
        gate = build_gate()
        violation = "party of 8 exceeds table capacity of 6"
        rules = RuleContext(violations=(violation,))

        decision = gate.evaluate(REQUEST, rules, conflict=False)

        assert decision.status == "PENDING_APPROVAL"
        assert violation in decision.reason


class TestConflict:
    def test_conflict_true_escalates_and_mentions_conflict(self):
        gate = build_gate()

        decision = gate.evaluate(REQUEST, RuleContext(), conflict=True)

        assert decision.status == "PENDING_APPROVAL"
        assert "conflict" in decision.reason.lower()


class TestMultipleViolations:
    def test_multiple_violations_all_reflected_in_reason(self):
        gate = build_gate()
        violation_a = "unknown practitioner: 'Dr. Nobody'"
        violation_b = "outside working hours: requested 20:00, hours are 09:00-17:00"
        rules = RuleContext(violations=(violation_a, violation_b))

        decision = gate.evaluate(REQUEST, rules, conflict=False)

        assert decision.status == "PENDING_APPROVAL"
        assert violation_a in decision.reason
        assert violation_b in decision.reason


class TestPriorityOrder:
    """Defensive behaviour: the real core never populates more than one
    signal at once, but the gate's own priority order must still hold if
    it did."""

    def test_missing_fields_wins_over_violations(self):
        gate = build_gate()
        rules = RuleContext(
            missing_fields=("practitioner",),
            violations=("unknown practitioner: 'Dr. Nobody'",),
        )

        decision = gate.evaluate(REQUEST, rules, conflict=True)

        assert decision.status == "PENDING_APPROVAL"
        assert "practitioner" in decision.reason
        assert "unknown practitioner" not in decision.reason

    def test_violations_win_over_conflict(self):
        gate = build_gate()
        violation = "unknown practitioner: 'Dr. Nobody'"
        rules = RuleContext(violations=(violation,))

        decision = gate.evaluate(REQUEST, rules, conflict=True)

        assert decision.status == "PENDING_APPROVAL"
        assert violation in decision.reason
        assert "conflict" not in decision.reason.lower()
