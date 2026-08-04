"""Tests for the SlotInfo widening (T9): resource_key + start.

Split out from `test_skillpack_interface.py` (T4) because this is
specifically about the T9 widening, not the abstract `SkillPack`
interface itself. Covers direct `SlotInfo` construction with all four
fields, and that `ClinicSkillPack.slot_rules()` (T5) now populates
`resource_key`/`start` correctly for a given request.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SlotInfo
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


class TestSlotInfoConstruction:
    """SlotInfo requires all four fields; no defaults were added for T9."""

    def test_construction_with_all_four_fields(self):
        start = datetime(2026, 8, 4, 10, 0)

        slot = SlotInfo(
            duration_minutes=30,
            capacity=1,
            resource_key="practitioner:Dr. A",
            start=start,
        )

        assert slot.duration_minutes == 30
        assert slot.capacity == 1
        assert slot.resource_key == "practitioner:Dr. A"
        assert slot.start == start

    def test_immutable(self):
        slot = SlotInfo(
            duration_minutes=30,
            capacity=1,
            resource_key="practitioner:Dr. A",
            start=datetime(2026, 8, 4, 10, 0),
        )

        with pytest.raises(FrozenInstanceError):
            slot.resource_key = "practitioner:Dr. B"

    def test_resource_key_and_start_are_required(self):
        with pytest.raises(TypeError):
            SlotInfo(duration_minutes=30, capacity=1)


class TestClinicSkillPackPopulatesNewFields:
    """ClinicSkillPack.slot_rules() (T5) populates resource_key and start."""

    def test_resource_key_derived_from_practitioner(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30, "Dr. B": 20})
        request = make_request(practitioner="Dr. A")

        slot = pack.slot_rules(request)

        assert slot.resource_key == "practitioner:Dr. A"

    def test_resource_key_differs_per_practitioner(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30, "Dr. B": 20})

        slot_a = pack.slot_rules(make_request(practitioner="Dr. A"))
        slot_b = pack.slot_rules(make_request(practitioner="Dr. B"))

        assert slot_a.resource_key != slot_b.resource_key
        assert slot_b.resource_key == "practitioner:Dr. B"

    def test_start_passed_through_unchanged_from_start_time_field(self):
        pack = ClinicSkillPack(practitioners={"Dr. A": 30})
        start_time = datetime(2026, 8, 4, 11, 15)
        request = make_request(practitioner="Dr. A", start_time=start_time)

        slot = pack.slot_rules(request)

        assert slot.start == start_time
        assert slot.start is start_time
