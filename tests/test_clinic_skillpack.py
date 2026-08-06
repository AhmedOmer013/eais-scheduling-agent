"""Tests for the clinic skill pack (skillpacks/clinic/pack.py).

Exercises `ClinicSkillPack` standalone against `BookingRequest` and the
`SkillPack`/`SlotInfo` interface, per the T5 brief. `SchedulingAgentCore`
(T6) and the approval gate (T7) don't exist yet -- no orchestration is
involved here.
"""

from datetime import datetime

import pytest

from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack


def make_request(practitioner="Dr. A", patient_name="Jane Doe", start_time=None, **extra_fields):
    """Build a clinic BookingRequest with sensible defaults for tests."""
    if start_time is None:
        start_time = datetime(2026, 8, 4, 10, 0)  # 10:00, within default hours
    fields = {
        "practitioner": practitioner,
        "patient_name": patient_name,
        "start_time": start_time,
    }
    fields.update(extra_fields)
    return BookingRequest(sector="clinic", fields=fields, raw_text="book an appointment")


class TestClinicSkillPackSatisfiesInterface:
    """ClinicSkillPack is a real SkillPack subclass."""

    def test_can_be_instantiated(self):
        pack = ClinicSkillPack()
        assert isinstance(pack, SkillPack)


class TestRequiredFieldsAndWorkingHours:
    def test_required_fields_default(self):
        pack = ClinicSkillPack()
        assert pack.required_fields == ["practitioner", "patient_name", "start_time"]

    def test_working_hours_default(self):
        pack = ClinicSkillPack()
        assert pack.working_hours == {"open": "09:00", "close": "17:00"}

    def test_working_hours_configurable(self):
        pack = ClinicSkillPack(working_hours={"open": "08:00", "close": "12:00"})
        assert pack.working_hours == {"open": "08:00", "close": "12:00"}

    def test_practitioners_default(self):
        pack = ClinicSkillPack()
        assert pack.practitioners == {"Dr. A": 30, "Dr. B": 20}

    def test_practitioners_configurable(self):
        pack = ClinicSkillPack(practitioners={"Dr. X": 45})
        assert pack.practitioners == {"Dr. X": 45}

    def test_practitioners_returns_a_copy_not_the_internal_dict(self):
        pack = ClinicSkillPack()
        pack.practitioners["Dr. Z"] = 99
        assert "Dr. Z" not in pack.practitioners


class TestSlotRules:
    def test_slot_rules_returns_practitioners_fixed_duration(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30, "Dr. B": 20})
        request = make_request(practitioner="Dr. A")

        slot = pack.slot_rules(request)

        assert isinstance(slot, SlotInfo)
        assert slot.duration_minutes == 30
        assert slot.capacity == 1

    def test_slot_rules_differs_per_practitioner(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30, "Dr. B": 20})
        request = make_request(practitioner="Dr. B")

        slot = pack.slot_rules(request)

        assert slot.duration_minutes == 20
        assert slot.capacity == 1

    def test_slot_rules_raises_for_unknown_practitioner(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30})
        request = make_request(practitioner="Dr. Nobody")

        with pytest.raises(ValueError):
            pack.slot_rules(request)


class TestValidate:
    def test_validate_returns_empty_list_for_valid_request(self):
        pack = ClinicSkillPack(
            practitioners={"Dr. A": 30},
            working_hours={"open": "09:00", "close": "17:00"},
        )
        request = make_request(
            practitioner="Dr. A", start_time=datetime(2026, 8, 4, 10, 0)
        )

        result = pack.validate(request)

        assert isinstance(result, list)
        assert result == []

    def test_validate_returns_violation_for_unknown_practitioner(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30})
        request = make_request(
            practitioner="Dr. Nobody", start_time=datetime(2026, 8, 4, 10, 0)
        )

        result = pack.validate(request)

        assert result == ["unknown practitioner: 'Dr. Nobody'"]

    def test_validate_returns_violation_for_outside_working_hours(self):
        pack = ClinicSkillPack(
            practitioners={"Dr. A": 30},
            working_hours={"open": "09:00", "close": "17:00"},
        )
        request = make_request(
            practitioner="Dr. A", start_time=datetime(2026, 8, 4, 20, 0)
        )

        result = pack.validate(request)

        assert result == [
            "outside working hours: requested 20:00, hours are 09:00-17:00"
        ]

    def test_validate_returns_both_violations_when_both_apply(self):
        pack = ClinicSkillPack(
            practitioners={"Dr. A": 30},
            working_hours={"open": "09:00", "close": "17:00"},
        )
        request = make_request(
            practitioner="Dr. Nobody", start_time=datetime(2026, 8, 4, 20, 0)
        )

        result = pack.validate(request)

        assert len(result) == 2
        assert any("unknown practitioner" in v for v in result)
        assert any("outside working hours" in v for v in result)

    def test_validate_close_time_is_exclusive(self):
        pack = ClinicSkillPack(
            practitioners={"Dr. A": 30},
            working_hours={"open": "09:00", "close": "17:00"},
        )
        request = make_request(
            practitioner="Dr. A", start_time=datetime(2026, 8, 4, 17, 0)
        )

        result = pack.validate(request)

        assert result == [
            "outside working hours: requested 17:00, hours are 09:00-17:00"
        ]

    def test_validate_open_time_is_inclusive(self):
        pack = ClinicSkillPack(
            practitioners={"Dr. A": 30},
            working_hours={"open": "09:00", "close": "17:00"},
        )
        request = make_request(
            practitioner="Dr. A", start_time=datetime(2026, 8, 4, 9, 0)
        )

        result = pack.validate(request)

        assert result == []

    def test_validate_raises_keyerror_for_missing_practitioner_field(self):
        pack = ClinicSkillPack()
        request = BookingRequest(
            sector="clinic",
            fields={"patient_name": "Jane Doe", "start_time": datetime(2026, 8, 4, 10, 0)},
            raw_text="book an appointment",
        )

        with pytest.raises(KeyError):
            pack.validate(request)


class TestConfirmationTemplate:
    def test_confirmation_template_returns_nonempty_string(self):
        pack = ClinicSkillPack()

        template = pack.confirmation_template()

        assert isinstance(template, str)
        assert len(template) > 0

    def test_confirmation_template_contains_expected_placeholders(self):
        pack = ClinicSkillPack()

        template = pack.confirmation_template()

        assert "{practitioner}" in template
        assert "{patient_name}" in template
        assert "{start_time}" in template
