"""Tests for the abstract SkillPack interface (skillpacks/base.py).

Defines a minimal, throwaway fake skill pack (not a real sector, not
committed under `skillpacks/`) to prove the interface is implementable,
and separately proves that `abc` actually enforces the contract rather
than it being documentation only.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo


class FakeSkillPack(SkillPack):
    """Minimal, throwaway skill pack used only to exercise the interface.

    Not a real sector -- deliberately trivial (no sector-specific rules
    beyond "the field must be present").
    """

    required_fields = ["thing"]
    working_hours = {"mon": ["09:00", "17:00"]}

    def validate(self, request):
        return [] if "thing" in request.fields else ["missing required field: thing"]

    def slot_rules(self, request):
        return SlotInfo(
            duration_minutes=30,
            capacity=1,
            resource_key="thing:widget",
            start=datetime(2026, 8, 4, 10, 0),
        )

    def confirmation_template(self):
        return "Your booking for {thing} is confirmed."


class TestFakeSkillPackSatisfiesInterface:
    """The fake pack can be instantiated and used, proving implementability."""

    def test_can_be_instantiated(self):
        pack = FakeSkillPack()
        assert isinstance(pack, SkillPack)

    def test_required_fields_and_working_hours_readable(self):
        pack = FakeSkillPack()
        assert pack.required_fields == ["thing"]
        assert pack.working_hours == {"mon": ["09:00", "17:00"]}

    def test_validate_returns_empty_list_for_valid_request(self):
        pack = FakeSkillPack()
        request = BookingRequest(
            sector="fake", fields={"thing": "widget"}, raw_text="book a widget"
        )

        result = pack.validate(request)

        assert isinstance(result, list)
        assert result == []

    def test_validate_returns_violations_for_invalid_request(self):
        pack = FakeSkillPack()
        request = BookingRequest(sector="fake", fields={}, raw_text="book something")

        result = pack.validate(request)

        assert isinstance(result, list)
        assert result == ["missing required field: thing"]

    def test_slot_rules_returns_slotinfo(self):
        pack = FakeSkillPack()
        request = BookingRequest(
            sector="fake", fields={"thing": "widget"}, raw_text="book a widget"
        )

        slot = pack.slot_rules(request)

        assert isinstance(slot, SlotInfo)
        assert slot.duration_minutes == 30
        assert slot.capacity == 1
        assert slot.resource_key == "thing:widget"
        assert slot.start == datetime(2026, 8, 4, 10, 0)

    def test_confirmation_template_returns_str(self):
        pack = FakeSkillPack()

        template = pack.confirmation_template()

        assert isinstance(template, str)
        assert len(template) > 0


class TestSlotInfo:
    """SlotInfo follows T2/T3's frozen-dataclass style."""

    def test_construction_and_field_access(self):
        slot = SlotInfo(
            duration_minutes=45,
            capacity=4,
            resource_key="table:5",
            start=datetime(2026, 8, 4, 12, 0),
        )

        assert slot.duration_minutes == 45
        assert slot.capacity == 4
        assert slot.resource_key == "table:5"
        assert slot.start == datetime(2026, 8, 4, 12, 0)

    def test_immutable(self):
        slot = SlotInfo(
            duration_minutes=45,
            capacity=4,
            resource_key="table:5",
            start=datetime(2026, 8, 4, 12, 0),
        )

        with pytest.raises(FrozenInstanceError):
            slot.duration_minutes = 60


class TestSkillPackCannotBeInstantiatedDirectly:
    """SkillPack is abstract; instantiating it directly must fail."""

    def test_instantiation_raises_type_error(self):
        with pytest.raises(TypeError):
            SkillPack()


class TestIncompleteSubclassCannotBeInstantiated:
    """Each abstract member is actually required, not just documented."""

    def test_subclass_missing_confirmation_template_cannot_instantiate(self):
        class IncompletePack(SkillPack):
            required_fields = []
            working_hours = {}

            def validate(self, request):
                return []

            def slot_rules(self, request):
                return SlotInfo(duration_minutes=10, capacity=1)

            # confirmation_template intentionally not implemented

        with pytest.raises(TypeError):
            IncompletePack()

    def test_subclass_missing_validate_cannot_instantiate(self):
        class IncompletePack(SkillPack):
            required_fields = []
            working_hours = {}

            def slot_rules(self, request):
                return SlotInfo(duration_minutes=10, capacity=1)

            def confirmation_template(self):
                return "confirmed"

            # validate intentionally not implemented

        with pytest.raises(TypeError):
            IncompletePack()

    def test_subclass_missing_required_fields_property_cannot_instantiate(self):
        class IncompletePack(SkillPack):
            working_hours = {}

            def validate(self, request):
                return []

            def slot_rules(self, request):
                return SlotInfo(duration_minutes=10, capacity=1)

            def confirmation_template(self):
                return "confirmed"

            # required_fields intentionally not implemented

        with pytest.raises(TypeError):
            IncompletePack()
