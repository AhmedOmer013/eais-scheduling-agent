"""Clinic skill pack: fixed-slot-length, named-practitioner booking.

The first concrete `SkillPack` (see `eais_scheduling_agent.skillpacks.base`),
proving that abstract interface is implementable for a real sector.

Sector shape, per the T5 brief (from PLAN.md / ARCHITECTURE.md):

- Each practitioner has a fixed appointment duration. A booking's resource
  footprint (`slot_rules`) is therefore that practitioner's fixed duration,
  with capacity always 1 (one practitioner, one patient at a time).
- A booking request must name a practitioner the clinic actually has;
  naming an unknown one is a validation violation.
- The clinic has a single daily working-hours window; a request outside it
  is a validation violation.

This module intentionally contains *all* clinic-specific logic. Nothing
sector-specific leaks into `core/`, `manifests/`, or `skillpacks/base.py`.
"""

from datetime import datetime, time
from typing import Dict, List, Optional

from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo

#: Fallback practitioner -> fixed slot length (minutes) used when the pack
#: is constructed without an explicit `practitioners` mapping. Sensible
#: defaults for tests and manual exploration; real configuration is T6's
#: job (reading a SectorManifest / sector config file), not this pack's.
_DEFAULT_PRACTITIONERS: Dict[str, int] = {"Dr. A": 30, "Dr. B": 20}

#: Fallback working-hours window. See `ClinicSkillPack` docstring for the
#: shape this dict takes.
_DEFAULT_WORKING_HOURS: Dict[str, str] = {"open": "09:00", "close": "17:00"}

#: Field names this pack reads from `BookingRequest.fields`. Also the
#: value returned by `required_fields` -- see that property's docstring
#: for why the two must stay in sync.
_PRACTITIONER_FIELD = "practitioner"
_PATIENT_NAME_FIELD = "patient_name"
_START_TIME_FIELD = "start_time"


def _parse_clock(value: str) -> time:
    """Parse an "HH:MM" 24-hour string into a `datetime.time`.

    Raises `ValueError` (via `int()` / unpacking) if `value` isn't
    "HH:MM" -- treated as a config error, not something callers need to
    catch, since `working_hours` is pack configuration, not request data.
    """
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


class ClinicSkillPack(SkillPack):
    """Fixed-slot-length, named-practitioner clinic skill pack.

    Configuration (constructor arguments, both optional with sensible
    defaults -- this pack does not read a `SectorManifest` file yet; that
    wiring belongs to T6's core orchestration):

    Args:
        practitioners: Maps practitioner name -> that practitioner's fixed
            appointment length in minutes, e.g.
            ``{"Dr. A": 30, "Dr. B": 20}``. Every practitioner a booking
            request may legally name must appear here; a name not present
            is an "unknown practitioner" validation violation. Defaults to
            a small two-practitioner example clinic.
        working_hours: The clinic's single daily open/close window, as
            ``{"open": "HH:MM", "close": "HH:MM"}`` in 24-hour clock time
            (e.g. ``{"open": "09:00", "close": "17:00"}``). Deliberately
            *not* per-day-of-week or per-practitioner -- the brief calls
            for a single daily window, and neither is needed yet. A
            request's `start_time` is compared against this window using
            time-of-day only (the calendar date is not otherwise checked
            by this pack). The close time is exclusive, the open time
            inclusive: a booking starting exactly at close is out of
            hours. Defaults to a 09:00-17:00 example clinic day.

    `BookingRequest.fields` this pack reads (also `required_fields`):
        practitioner (str): Name of the practitioner the booking is for.
            Must be a key in `practitioners`.
        patient_name (str): Name of the patient. Not otherwise validated
            by this pack (no format/content constraints); used only for
            slot bookkeeping and to fill `confirmation_template()`.
        start_time (datetime.datetime): The requested appointment start.
            A `datetime.datetime` object (not an ISO string) -- intake is
            expected to have already parsed free text into structured
            data by the time a `BookingRequest` reaches this pack, per
            `BookingRequest.fields`'s docstring ("Structured fields
            extracted by intake"). Only the time-of-day component is used
            for the working-hours check; slot end time (for future
            conflict-checking, not in this task's scope) would be
            `start_time + duration_minutes`.

    Precondition / scope note: `validate()` and `slot_rules()` assume
    `request.fields` already contains these three keys. Checking that a
    `BookingRequest` contains all `required_fields` before it reaches a
    skill pack is explicitly out of this task's scope (a future core/gate
    responsibility -- see the T5 brief's scope boundary). If a key is
    missing, both methods let the resulting `KeyError` propagate (with a
    clarifying message) rather than inventing a violation string for it.
    """

    def __init__(
        self,
        practitioners: Optional[Dict[str, int]] = None,
        working_hours: Optional[Dict[str, str]] = None,
    ) -> None:
        self._practitioners: Dict[str, int] = (
            dict(practitioners) if practitioners is not None else dict(_DEFAULT_PRACTITIONERS)
        )
        self._working_hours: Dict[str, str] = (
            dict(working_hours) if working_hours is not None else dict(_DEFAULT_WORKING_HOURS)
        )
        # Parsed once at construction so validate() doesn't re-parse the
        # same two strings on every call; config errors (bad "HH:MM"
        # shape) surface immediately at construction time instead of on
        # the first booking.
        self._open_time: time = _parse_clock(self._working_hours["open"])
        self._close_time: time = _parse_clock(self._working_hours["close"])

    @property
    def required_fields(self) -> List[str]:
        """Fields a clinic BookingRequest must contain.

        Kept in exact sync with the fields `validate()`/`slot_rules()`
        actually read: `practitioner`, `patient_name`, `start_time`.
        """
        return [_PRACTITIONER_FIELD, _PATIENT_NAME_FIELD, _START_TIME_FIELD]

    @property
    def working_hours(self) -> Dict:
        """The clinic's configured daily open/close window.

        Returns the ``{"open": "HH:MM", "close": "HH:MM"}`` dict as
        configured (or the default) -- see the class docstring for the
        shape and semantics.
        """
        return dict(self._working_hours)

    @property
    def practitioners(self) -> Dict[str, int]:
        """The clinic's configured practitioner -> fixed appointment length (minutes) mapping.

        Returns a copy of the ``{practitioner_name: duration_minutes}``
        dict as configured (or the default) -- mirrors `working_hours`'s
        read-only, defensive-copy convention.
        """
        return dict(self._practitioners)

    def _extract_fields(self, request: BookingRequest):
        """Pull the three fields this pack needs, or raise a clear KeyError.

        Deliberately not a violation string: an absent field here means
        the request never should have reached this pack (that's the
        future core/gate's job to catch) -- see class docstring's
        "Precondition / scope note".
        """
        try:
            practitioner = request.fields[_PRACTITIONER_FIELD]
            start_time = request.fields[_START_TIME_FIELD]
        except KeyError as exc:
            raise KeyError(
                f"ClinicSkillPack requires field {exc}, but it is missing from "
                f"request.fields; missing-field checking belongs to the future "
                f"core/gate, not this sector pack's validate()/slot_rules()"
            ) from exc
        return practitioner, start_time

    def validate(self, request: BookingRequest) -> List[str]:
        """Check sector-specific constraints only.

        Two checks, per the T5 brief's scope boundary:

        1. The named practitioner is one this clinic actually has.
        2. The requested start time falls within working hours.

        Does NOT check that `request.fields` contains the required keys
        at all -- see `_extract_fields` / class docstring.

        Returns:
            A list of specific, human-readable violation strings (empty
            if the request satisfies both checks).
        """
        practitioner, start_time = self._extract_fields(request)

        violations: List[str] = []

        if practitioner not in self._practitioners:
            violations.append(f"unknown practitioner: {practitioner!r}")

        time_of_day = start_time.time()
        if not (self._open_time <= time_of_day < self._close_time):
            violations.append(
                f"outside working hours: requested {time_of_day.strftime('%H:%M')}, "
                f"hours are {self._working_hours['open']}-{self._working_hours['close']}"
            )

        return violations

    def slot_rules(self, request: BookingRequest) -> SlotInfo:
        """Return the fixed slot length for the request's practitioner.

        Capacity is always 1 (one practitioner, one patient at a time),
        per `SlotInfo`'s docstring in `skillpacks/base.py`. `resource_key`
        is `f"practitioner:{practitioner}"` -- opaque to everything
        downstream (T9's `BookingStore` only ever compares it for
        equality), so the exact format is this pack's own choice; the
        `"practitioner:"` prefix just keeps it human-readable for
        debugging. `start` is the request's already-validated
        `start_time`, passed through unchanged via `_extract_fields`
        rather than re-derived.

        Raises:
            KeyError: if `request.fields` is missing `practitioner` or
                `start_time` (see `_extract_fields`).
            ValueError: if `practitioner` is not one this pack knows
                about. `validate()` is expected to have already caught
                this as an "unknown practitioner" violation before
                `slot_rules()` is called on the same request; this is a
                defensive guard, not this method's primary validation
                path.
        """
        practitioner, start_time = self._extract_fields(request)

        if practitioner not in self._practitioners:
            raise ValueError(
                f"cannot compute slot rules for unknown practitioner: {practitioner!r}"
            )

        return SlotInfo(
            duration_minutes=self._practitioners[practitioner],
            capacity=1,
            resource_key=f"practitioner:{practitioner}",
            start=start_time,
        )

    def confirmation_template(self) -> str:
        """Return the CONFIRMED-decision message template.

        `.format()`-style placeholders matching this pack's field names,
        for a future renderer (not built in this task) to fill in from
        `request.fields`.
        """
        return (
            "Confirmed: {patient_name} with {practitioner} at {start_time}."
        )
