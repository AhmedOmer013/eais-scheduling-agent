"""Tests for the restaurant skill pack (skillpacks/restaurant/pack.py).

Exercises `RestaurantSkillPack` standalone against `BookingRequest` and
the `SkillPack`/`SlotInfo` interface, per the T10 brief. Mirrors
`test_clinic_skillpack.py`'s structure, adapted for restaurant's
flexible-duration, table-assignment shape.
"""

from datetime import datetime

import pytest

from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack


def make_request(party_size=2, customer_name="Jane Doe", start_time=None, **extra_fields):
    """Build a restaurant BookingRequest with sensible defaults for tests."""
    if start_time is None:
        start_time = datetime(2026, 8, 4, 18, 0)  # 18:00, within default hours
    fields = {
        "party_size": party_size,
        "customer_name": customer_name,
        "start_time": start_time,
    }
    fields.update(extra_fields)
    return BookingRequest(sector="restaurant", fields=fields, raw_text="book a table")


class TestRestaurantSkillPackSatisfiesInterface:
    """RestaurantSkillPack is a real SkillPack subclass."""

    def test_can_be_instantiated(self):
        pack = RestaurantSkillPack()
        assert isinstance(pack, SkillPack)


class TestRequiredFieldsAndWorkingHours:
    def test_required_fields_default(self):
        pack = RestaurantSkillPack()
        assert pack.required_fields == ["party_size", "customer_name", "start_time"]

    def test_working_hours_default(self):
        pack = RestaurantSkillPack()
        assert pack.working_hours == {"open": "11:00", "close": "22:00"}

    def test_working_hours_configurable(self):
        pack = RestaurantSkillPack(working_hours={"open": "17:00", "close": "23:00"})
        assert pack.working_hours == {"open": "17:00", "close": "23:00"}


class TestSlotRulesTableAssignment:
    """slot_rules() picks the smallest-fitting table, deterministically."""

    def test_picks_smallest_fitting_table_for_small_party(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}
        )
        request = make_request(party_size=3)

        slot = pack.slot_rules(request)

        assert isinstance(slot, SlotInfo)
        assert slot.resource_key == "table:T3"

    def test_picks_smallest_fitting_table_for_large_party(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}
        )
        request = make_request(party_size=8)

        slot = pack.slot_rules(request)

        assert slot.resource_key == "table:T5"

    def test_tie_break_picks_lexicographically_smallest_table_id(self):
        # T1 and T2 both have capacity 2 -- a party of 2 (or fewer) fits
        # both equally well; the documented tie-break is lowest table id.
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}
        )
        request = make_request(party_size=2)

        slot = pack.slot_rules(request)

        assert slot.resource_key == "table:T1"

    def test_tie_break_is_stable_regardless_of_config_dict_order(self):
        # Same tied tables, declared in the opposite order -- the tie
        # break must be by id, not by insertion/iteration order.
        pack = RestaurantSkillPack(
            tables={"T2": 2, "T1": 2, "T3": 4, "T4": 6, "T5": 8}
        )
        request = make_request(party_size=2)

        slot = pack.slot_rules(request)

        assert slot.resource_key == "table:T1"

    def test_capacity_on_slotinfo_is_party_size_not_table_capacity(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}
        )
        request = make_request(party_size=3)

        slot = pack.slot_rules(request)

        # Party of 3 is assigned table T3 (capacity 4), but SlotInfo.capacity
        # reports the *demand* (party_size=3), not the table's seat count.
        assert slot.resource_key == "table:T3"
        assert slot.capacity == 3

    def test_slot_rules_raises_when_no_table_fits(self):
        pack = RestaurantSkillPack(tables={"T1": 2, "T2": 4})
        request = make_request(party_size=12)

        with pytest.raises(ValueError):
            pack.slot_rules(request)


class TestSlotRulesFlexibleDuration:
    """slot_rules() produces genuinely different durations per party size."""

    def test_duration_grows_with_party_size(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}
        )

        small = pack.slot_rules(make_request(party_size=2))
        medium = pack.slot_rules(make_request(party_size=4))
        large = pack.slot_rules(make_request(party_size=8))

        assert small.duration_minutes == 60
        assert medium.duration_minutes == 90
        assert large.duration_minutes == 150
        # Not a constant: strictly increasing with party size.
        assert small.duration_minutes < medium.duration_minutes < large.duration_minutes


class TestValidate:
    def test_validate_returns_empty_list_for_valid_request(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 4},
            working_hours={"open": "11:00", "close": "22:00"},
        )
        request = make_request(party_size=2, start_time=datetime(2026, 8, 4, 18, 0))

        result = pack.validate(request)

        assert isinstance(result, list)
        assert result == []

    def test_validate_returns_violation_for_over_capacity_party(self):
        pack = RestaurantSkillPack(tables={"T1": 2, "T2": 4, "T3": 8})
        request = make_request(
            party_size=12, start_time=datetime(2026, 8, 4, 18, 0)
        )

        result = pack.validate(request)

        assert result == ["party of 12 exceeds largest table capacity of 8"]

    def test_validate_returns_violation_for_outside_working_hours(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 4},
            working_hours={"open": "11:00", "close": "22:00"},
        )
        request = make_request(
            party_size=2, start_time=datetime(2026, 8, 4, 9, 0)
        )

        result = pack.validate(request)

        assert result == [
            "outside working hours: requested 09:00, hours are 11:00-22:00"
        ]

    def test_validate_returns_both_violations_when_both_apply(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 4},
            working_hours={"open": "11:00", "close": "22:00"},
        )
        request = make_request(
            party_size=12, start_time=datetime(2026, 8, 4, 9, 0)
        )

        result = pack.validate(request)

        assert len(result) == 2
        assert any("exceeds largest table capacity" in v for v in result)
        assert any("outside working hours" in v for v in result)

    def test_validate_close_time_is_exclusive(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 4},
            working_hours={"open": "11:00", "close": "22:00"},
        )
        request = make_request(
            party_size=2, start_time=datetime(2026, 8, 4, 22, 0)
        )

        result = pack.validate(request)

        assert result == [
            "outside working hours: requested 22:00, hours are 11:00-22:00"
        ]

    def test_validate_open_time_is_inclusive(self):
        pack = RestaurantSkillPack(
            tables={"T1": 2, "T2": 4},
            working_hours={"open": "11:00", "close": "22:00"},
        )
        request = make_request(
            party_size=2, start_time=datetime(2026, 8, 4, 11, 0)
        )

        result = pack.validate(request)

        assert result == []

    def test_validate_raises_keyerror_for_missing_party_size_field(self):
        pack = RestaurantSkillPack()
        request = BookingRequest(
            sector="restaurant",
            fields={"customer_name": "Jane Doe", "start_time": datetime(2026, 8, 4, 18, 0)},
            raw_text="book a table",
        )

        with pytest.raises(KeyError):
            pack.validate(request)


class TestConfirmationTemplate:
    def test_confirmation_template_returns_nonempty_string(self):
        pack = RestaurantSkillPack()

        template = pack.confirmation_template()

        assert isinstance(template, str)
        assert len(template) > 0

    def test_confirmation_template_contains_expected_placeholders(self):
        pack = RestaurantSkillPack()

        template = pack.confirmation_template()

        assert "{customer_name}" in template
        assert "{party_size}" in template
        assert "{start_time}" in template
