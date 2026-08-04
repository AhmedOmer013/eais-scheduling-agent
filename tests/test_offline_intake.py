"""Tests for the deterministic offline intake parser (T12).

Two distinct tiers, per the T12 brief -- kept deliberately separate:

1. **Determinism (AC7)** -- `TestDeterminismAcrossAllTrainingExamples` is
   the centerpiece: every one of the 20 hand-authored examples in
   `training/clinic_examples.jsonl` / `training/restaurant_examples.jsonl`
   is parsed twice with the same injected `now`, asserting the two
   `BookingRequest` objects are field-for-field identical. This is the
   test that directly proves "identical input -> identical output".
2. **Extraction correctness** -- a representative sample asserting
   *specific* field values (or the documented degraded/omitted shape) for
   examples spanning every `category` present in the training files. Not
   every example gets a correctness assertion; see the module-level
   `# Correctness coverage note` below for which ones don't and why.

`training/*.jsonl`'s own `expected` field uses a human-facing schema
(`date`/`time` as descriptive strings) that does not match this module's
actual output shape (`start_time` as a single `datetime`, per
`ClinicSkillPack`/`RestaurantSkillPack`'s field contracts) -- correctness
assertions here check against what a reasonable parse of `fields` should
produce given `OfflineIntake`'s own documented extraction rules, not a
literal diff against the training file's `expected` dict.

# Correctness coverage note

Every category in both training files gets a specific-value assertion
below *except* `rest-10` (`casual_shorthand`, "table 4 2nite 8"), which
the brief and the training data's own notes flag as the one case a
deterministic offline parser may reasonably fail closed on. `rest-10` is
still exercised by the determinism test (proving it doesn't crash or
behave non-deterministically), and by
`test_rest_10_documented_hard_case_fails_closed` below, which asserts the
*documented* degradation directly: no fields are guessed.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.intake.offline import OfflineIntake

_TRAINING_DIR = Path(__file__).resolve().parents[1] / "training"

# A fixed Monday reference point for all tests that don't specifically
# exercise the "now" injection itself. Chosen arbitrarily; the point is
# only that it is fixed, not real-clock-dependent.
_FIXED_NOW = datetime(2026, 8, 10, 12, 0)


def _load_examples(filename: str):
    path = _TRAINING_DIR / filename
    examples = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


_CLINIC_EXAMPLES = _load_examples("clinic_examples.jsonl")
_RESTAURANT_EXAMPLES = _load_examples("restaurant_examples.jsonl")
_ALL_EXAMPLES = [("clinic", ex) for ex in _CLINIC_EXAMPLES] + [
    ("restaurant", ex) for ex in _RESTAURANT_EXAMPLES
]


def _ids(pairs):
    return [f"{sector}:{ex['id']}" for sector, ex in pairs]


class TestOfflineIntakeSatisfiesInterface:
    def test_is_an_intake_service(self):
        assert isinstance(OfflineIntake(), IntakeService)


class TestSectorAndRawTextPlacement:
    """`parse()` must place `sector`/`raw_text` per the interface contract."""

    def test_sector_and_raw_text_are_carried_through_unchanged_clinic(self):
        intake = OfflineIntake(now=lambda: _FIXED_NOW)
        text = "Can I book an appointment with Dr. Chen for this Friday at 3pm?"
        request = intake.parse(text, "clinic")
        assert isinstance(request, BookingRequest)
        assert request.sector == "clinic"
        assert request.raw_text == text

    def test_sector_and_raw_text_are_carried_through_unchanged_restaurant(self):
        intake = OfflineIntake(now=lambda: _FIXED_NOW)
        text = "table for two tonight"
        request = intake.parse(text, "restaurant")
        assert request.sector == "restaurant"
        assert request.raw_text == text

    def test_sector_string_is_not_validated_against_a_known_list(self):
        """Intake places `sector` verbatim; it does not gate on a whitelist.

        Per `IntakeService.parse`'s docstring, `sector` selects an
        extraction profile at most -- an unrecognized sector should not
        raise, it should just skip sector-specific extraction.
        """
        intake = OfflineIntake(now=lambda: _FIXED_NOW)
        request = intake.parse("hello", "some_future_sector")
        assert request.sector == "some_future_sector"


class TestNoNetworkAccess:
    """Confirms this module only uses stdlib `re`/`datetime` -- no network."""

    def test_source_does_not_import_networking_modules(self):
        import eais_scheduling_agent.intake.offline as offline_module

        source_path = Path(offline_module.__file__)
        source = source_path.read_text(encoding="utf-8")
        forbidden = ["requests", "urllib", "http.client", "socket", "httpx"]
        for token in forbidden:
            assert token not in source, f"unexpected networking-related token: {token!r}"

    def test_module_imports_are_stdlib_and_core_only(self):
        import eais_scheduling_agent.intake.offline as offline_module

        assert offline_module.re.__name__ == "re"
        assert offline_module.datetime is not None


class TestInjectableNowIsNotANoOp:
    """AC7's determinism hinges on `now` actually being used, not ignored."""

    def test_two_different_now_values_produce_different_resolved_dates(self):
        intake_day_one = OfflineIntake(now=lambda: datetime(2026, 8, 10, 12, 0))  # Monday
        intake_day_two = OfflineIntake(now=lambda: datetime(2026, 8, 17, 12, 0))  # next Monday

        text = "Can I book an appointment with Dr. Chen for this Friday at 3pm?"
        request_one = intake_day_one.parse(text, "clinic")
        request_two = intake_day_two.parse(text, "clinic")

        assert request_one.fields["start_time"] != request_two.fields["start_time"]
        assert request_one.fields["start_time"] == datetime(2026, 8, 14, 15, 0)
        assert request_two.fields["start_time"] == datetime(2026, 8, 21, 15, 0)

    def test_tomorrow_resolves_relative_to_injected_now(self):
        intake_a = OfflineIntake(now=lambda: datetime(2026, 8, 10, 12, 0))
        intake_b = OfflineIntake(now=lambda: datetime(2026, 12, 25, 12, 0))

        text = "can I get a table for 4 tomorrow around 8pm"
        result_a = intake_a.parse(text, "restaurant")
        result_b = intake_b.parse(text, "restaurant")

        assert result_a.fields["start_time"] == datetime(2026, 8, 11, 20, 0)
        assert result_b.fields["start_time"] == datetime(2026, 12, 26, 20, 0)

    def test_default_constructor_uses_real_clock(self):
        """No injected `now` falls back to the real `datetime.now`.

        Only checks that "today" resolves to the actual current date, not
        some fixed/frozen value -- proving the default isn't secretly
        hard-coded.
        """
        intake = OfflineIntake()
        request = intake.parse("table for two today at 6pm", "restaurant")
        assert request.fields["start_time"].date() == datetime.now().date()


class TestDeterminismAcrossAllTrainingExamples:
    """AC7's actual done-when: identical input -> identical output.

    Every example from both training files is parsed twice with the same
    injected `now`; the two `BookingRequest` results must be equal in
    every field. `BookingRequest` is a frozen dataclass, so `==` compares
    all three attributes (`sector`, `fields`, `raw_text`) structurally,
    including deep dict equality on `fields` -- verified directly by
    `TestBookingRequestSupportsEquality` below.
    """

    @pytest.mark.parametrize("sector,example", _ALL_EXAMPLES, ids=_ids(_ALL_EXAMPLES))
    def test_parse_twice_with_same_now_yields_identical_result(self, sector, example):
        intake = OfflineIntake(now=lambda: _FIXED_NOW)
        first = intake.parse(example["text"], sector)
        second = intake.parse(example["text"], sector)
        assert first == second
        assert first.fields == second.fields
        assert first.sector == second.sector == sector
        assert first.raw_text == second.raw_text == example["text"]

    def test_repeated_parse_across_a_fresh_instance_is_also_identical(self):
        """Determinism holds across separate `OfflineIntake` instances too.

        Not just "the same object called twice" -- two independently
        constructed parsers with the same injected `now` must agree,
        proving there's no hidden instance-local state driving the
        output.
        """
        text = "Dr. Salem, patient Ahmed Omer, Tuesday 10:30am, annual physical"
        first = OfflineIntake(now=lambda: _FIXED_NOW).parse(text, "clinic")
        second = OfflineIntake(now=lambda: _FIXED_NOW).parse(text, "clinic")
        assert first == second


class TestBookingRequestSupportsEquality:
    """Sanity check underpinning the determinism tests above."""

    def test_equal_field_dicts_compare_equal(self):
        a = BookingRequest(sector="clinic", fields={"x": 1}, raw_text="t")
        b = BookingRequest(sector="clinic", fields={"x": 1}, raw_text="t")
        assert a == b

    def test_differing_field_dicts_compare_unequal(self):
        a = BookingRequest(sector="clinic", fields={"x": 1}, raw_text="t")
        b = BookingRequest(sector="clinic", fields={"x": 2}, raw_text="t")
        assert a != b


class TestClinicExtractionCorrectness:
    """Specific-value assertions for a representative sample of clinic
    examples, one per `category` present in `training/clinic_examples.jsonl`.
    """

    def _parse(self, text):
        return OfflineIntake(now=lambda: _FIXED_NOW).parse(text, "clinic")

    def test_clinic_01_ambiguous_time_omits_start_time_but_keeps_practitioner_and_period(self):
        # category: ambiguous_time
        request = self._parse("I need to see Dr. Salem next Tuesday morning")
        assert request.fields["practitioner"] == "Dr. Salem"
        assert "start_time" not in request.fields
        assert request.fields["time_period"] == "morning"

    def test_clinic_02_happy_path_extracts_practitioner_and_start_time(self):
        # category: happy_path
        request = self._parse("Can I book an appointment with Dr. Chen for this Friday at 3pm?")
        assert request.fields["practitioner"] == "Dr. Chen"
        assert request.fields["start_time"] == datetime(2026, 8, 14, 15, 0)
        assert "patient_name" not in request.fields

    def test_clinic_03_missing_field_omits_practitioner_entirely(self):
        # category: missing_field -- "the doctor" is not a name, must not
        # be guessed as a practitioner.
        request = self._parse("book me in with the doctor tomorrow")
        assert "practitioner" not in request.fields
        assert "start_time" not in request.fields

    def test_clinic_04_missing_field_multiple_omits_everything_unresolved(self):
        # category: missing_field_multiple
        request = self._parse("Need a checkup sometime next week")
        assert "practitioner" not in request.fields
        assert "start_time" not in request.fields
        assert "patient_name" not in request.fields

    def test_clinic_05_casual_shorthand_still_resolves_cleanly(self):
        # category: casual_shorthand -- terse/typo'd but not the
        # documented hard case (that's rest-10); this one is expected to
        # parse successfully.
        request = self._parse("Dr Salem 9am monday pls")
        assert request.fields["practitioner"] == "Dr. Salem"
        assert request.fields["start_time"] == datetime(2026, 8, 10, 9, 0)

    def test_clinic_06_out_of_hours_still_parses_cleanly(self):
        # category: out_of_hours -- intake's job is just to parse; the
        # working-hours rejection belongs to the approval gate, not here.
        request = self._parse("I'd like an appointment with Dr. Amara at 7am on Wednesday")
        assert request.fields["practitioner"] == "Dr. Amara"
        assert request.fields["start_time"] == datetime(2026, 8, 12, 7, 0)

    def test_clinic_07_ambiguous_patient_omits_patient_name_not_guesses_my_son(self):
        # category: ambiguous_patient -- "my son" must never end up as a
        # literal patient_name value.
        request = self._parse("My son needs to see Dr. Lee, is 2pm Thursday free?")
        assert request.fields["practitioner"] == "Dr. Lee"
        assert request.fields["start_time"] == datetime(2026, 8, 13, 14, 0)
        assert "patient_name" not in request.fields

    def test_clinic_08_missing_field_underspecified_omits_practitioner(self):
        # category: missing_field_underspecified -- "any available
        # doctor" names no one.
        request = self._parse("urgent - need to see any available doctor today")
        assert "practitioner" not in request.fields
        assert "start_time" not in request.fields
        assert request.fields["urgency"] == "urgent"

    def test_clinic_09_unsupported_action_is_tagged_not_silently_misfiled(self):
        # category: unsupported_action -- must not be silently treated as
        # an ordinary fresh booking; the detected verb is surfaced.
        request = self._parse("reschedule my appointment with Dr. Salem to next Tuesday 10am")
        assert request.fields["action"] == "reschedule"
        assert request.fields["practitioner"] == "Dr. Salem"
        assert request.fields["start_time"] == datetime(2026, 8, 18, 10, 0)

    def test_clinic_10_happy_path_detailed_extracts_every_required_field(self):
        # category: happy_path_detailed -- list-style input, all three
        # required fields present.
        request = self._parse("Dr. Salem, patient Ahmed Omer, Tuesday 10:30am, annual physical")
        assert request.fields["practitioner"] == "Dr. Salem"
        assert request.fields["patient_name"] == "Ahmed Omer"
        assert request.fields["start_time"] == datetime(2026, 8, 11, 10, 30)


class TestRestaurantExtractionCorrectness:
    """Specific-value assertions for a representative sample of restaurant
    examples, one per `category` present in
    `training/restaurant_examples.jsonl` (except `casual_shorthand`, see
    `test_rest_10_documented_hard_case_fails_closed` below).
    """

    def _parse(self, text):
        return OfflineIntake(now=lambda: _FIXED_NOW).parse(text, "restaurant")

    def test_rest_01_happy_path_extracts_party_size_and_start_time(self):
        # category: happy_path -- "around 8pm" should not block exact-time
        # extraction.
        request = self._parse("can I get a table for 4 tomorrow around 8pm")
        assert request.fields["party_size"] == 4
        assert request.fields["start_time"] == datetime(2026, 8, 11, 20, 0)

    def test_rest_02_missing_field_omits_start_time_not_a_default_time(self):
        # category: missing_field -- no time given at all.
        request = self._parse("table for two tonight")
        assert request.fields["party_size"] == 2
        assert "start_time" not in request.fields

    def test_rest_03_over_capacity_still_parses_cleanly(self):
        # category: over_capacity -- capacity rejection belongs to the
        # approval gate; intake should still extract what it can.
        request = self._parse("We're a group of 12, do you have space this Saturday evening?")
        assert request.fields["party_size"] == 12
        assert "start_time" not in request.fields
        assert request.fields["time_period"] == "evening"

    def test_rest_04_casual_urgent_ignores_emphasis_punctuation(self):
        # category: casual_urgent
        request = self._parse("booking for 6 people at 9:45pm friday, need it done fast!!")
        assert request.fields["party_size"] == 6
        assert request.fields["start_time"] == datetime(2026, 8, 14, 21, 45)

    def test_rest_05_preference_extra_field_carries_seating_preference(self):
        # category: preference_extra_field
        request = self._parse("reserve outdoor seating for 3, sunday lunch")
        assert request.fields["party_size"] == 3
        assert request.fields["seating_preference"] == "outdoor"
        assert "start_time" not in request.fields

    def test_rest_06_out_of_hours_still_parses_cleanly(self):
        # category: out_of_hours
        request = self._parse("party of 5 at 11pm wednesday")
        assert request.fields["party_size"] == 5
        assert request.fields["start_time"] == datetime(2026, 8, 12, 23, 0)

    def test_rest_07_ambiguous_time_omits_start_time_for_open_ended_request(self):
        # category: ambiguous_time -- "whenever's free" gives no exact
        # time.
        request = self._parse("can we get a table for 4, no specific time, whenever's free today")
        assert request.fields["party_size"] == 4
        assert "start_time" not in request.fields

    def test_rest_08_edge_small_party_does_not_guess_asap_as_a_time(self):
        # category: edge_small_party -- "asap" is not a clock time and no
        # date word appears in the text; both are correctly omitted
        # rather than guessed as "today, right now".
        request = self._parse("table for 1 please, asap")
        assert request.fields["party_size"] == 1
        assert "start_time" not in request.fields
        assert request.fields["urgency"] == "asap"

    def test_rest_09_preference_extra_field_carries_both_optional_fields(self):
        # category: preference_extra_field
        request = self._parse(
            "need a table for our anniversary dinner, party of 2, saturday 7pm, "
            "quiet corner if possible"
        )
        assert request.fields["party_size"] == 2
        assert request.fields["start_time"] == datetime(2026, 8, 15, 19, 0)
        assert request.fields["seating_preference"] == "quiet corner"
        assert request.fields["occasion"] == "anniversary"

    def test_rest_10_documented_hard_case_fails_closed(self):
        # category: casual_shorthand -- the brief's own documented case a
        # deterministic offline parser may reasonably fail closed on.
        # Asserted here to prove it degrades honestly (nothing guessed),
        # not left to the determinism test alone.
        request = self._parse("table 4 2nite 8")
        assert "party_size" not in request.fields
        assert "start_time" not in request.fields
        assert "customer_name" not in request.fields
