"""Restaurant skill pack: flexible-duration, party-size/table booking.

The second concrete `SkillPack` (see `eais_scheduling_agent.skillpacks.base`),
proving the abstract interface generalizes to a second sector without any
change to `core/`, `manifests/manifest.py`, or `skillpacks/base.py` -- see
this task's brief (T10) for why that is the point of this task.

Sector shape, per the T10 brief:

- The restaurant has a fixed set of tables, each with a seat capacity. A
  booking request names a party size, not a table; `slot_rules()`
  deterministically assigns the smallest table that fits the party (see
  `_pick_table` and the "Known limitation" section of this class's
  docstring for why this is a static assignment, not a live-availability
  search).
- A booking's duration is *not* fixed (unlike clinic's per-practitioner
  fixed duration) -- it grows with party size, per `_compute_duration`.
- A party too large for every configured table is a validation violation
  ("over-capacity"), distinct from clinic's "unknown practitioner".
- The restaurant has a single daily working-hours window, checked the same
  way `ClinicSkillPack` checks it.

This module intentionally contains *all* restaurant-specific logic.
Nothing sector-specific leaks into `core/`, `manifests/`, or
`skillpacks/base.py`.
"""

from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo

#: Fallback table -> seat capacity mapping used when the pack is
#: constructed without an explicit `tables` mapping. Deliberately includes
#: a tied pair (T1/T2, both capacity 2) so the smallest-fitting-table
#: tie-break rule (see `_pick_table`) has a real case to exercise even
#: with defaults. Real configuration is a future core/manifest-wiring
#: task's job, not this pack's.
_DEFAULT_TABLES: Dict[str, int] = {"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}

#: Fallback working-hours window. See `RestaurantSkillPack` docstring for
#: the shape this dict takes.
_DEFAULT_WORKING_HOURS: Dict[str, str] = {"open": "11:00", "close": "22:00"}

#: Flexible-duration formula constants (see `_compute_duration`). A party
#: at or below the threshold gets the base duration; each guest beyond the
#: threshold adds a fixed number of extra minutes. Not sector config
#: (no constructor argument) -- a fixed, documented house rule, same
#: status as the tie-break rule below.
_BASE_DURATION_MINUTES = 60
_DURATION_THRESHOLD_PARTY_SIZE = 2
_EXTRA_MINUTES_PER_ADDITIONAL_GUEST = 15

#: Field names this pack reads from `BookingRequest.fields`. Also the
#: value returned by `required_fields` -- see that property's docstring
#: for why the two must stay in sync.
_PARTY_SIZE_FIELD = "party_size"
_CUSTOMER_NAME_FIELD = "customer_name"
_START_TIME_FIELD = "start_time"


def _parse_clock(value: str) -> time:
    """Parse an "HH:MM" 24-hour string into a `datetime.time`.

    Raises `ValueError` (via `int()` / unpacking) if `value` isn't
    "HH:MM" -- treated as a config error, not something callers need to
    catch, since `working_hours` is pack configuration, not request data.
    """
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


class RestaurantSkillPack(SkillPack):
    """Flexible-duration, table-assignment restaurant skill pack.

    Configuration (constructor arguments, both optional with sensible
    defaults -- this pack does not read a `SectorManifest` file yet; that
    wiring belongs to a future core-orchestration task, same as
    `ClinicSkillPack`):

    Args:
        tables: Maps table id -> that table's seat capacity, e.g.
            ``{"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}``. Defaults to
            a small five-table example restaurant (see `_DEFAULT_TABLES`).
        working_hours: The restaurant's single daily open/close window, as
            ``{"open": "HH:MM", "close": "HH:MM"}`` in 24-hour clock time.
            Same shape and same time-of-day-only semantics as
            `ClinicSkillPack.working_hours` (close exclusive, open
            inclusive). Defaults to an 11:00-22:00 example restaurant day.

    `BookingRequest.fields` this pack reads (also `required_fields`):
        party_size (int): Number of guests in the party.
        customer_name (str): Name the booking is under. Not otherwise
            validated by this pack; used only for slot bookkeeping and to
            fill `confirmation_template()`.
        start_time (datetime.datetime): The requested booking start. A
            `datetime.datetime` object (not an ISO string), same
            convention as `ClinicSkillPack.start_time`. Only the
            time-of-day component is used for the working-hours check.

    Precondition / scope note: `validate()` and `slot_rules()` assume
    `request.fields` already contains these three keys. Checking that a
    `BookingRequest` contains all `required_fields` before it reaches a
    skill pack is out of this pack's scope (a core/gate responsibility,
    same scope boundary `ClinicSkillPack` documents). If a key is missing,
    both methods let the resulting `KeyError` propagate rather than
    inventing a violation string for it.

    Table-assignment design and its known, accepted limitation:
        `SchedulingAgentCore.handle()` calls `slot_rules(request)` *before*
        `store.check_conflict(request, slot)` (see T6's orchestration
        flow), so `slot_rules()` has no visibility into which tables are
        currently booked -- it cannot do a live-availability table search.
        Instead, `_pick_table` deterministically assigns the smallest
        configured table whose capacity is >= the request's `party_size`
        (ties broken by lexicographically-smallest table id), and
        `resource_key` becomes that table's identifier. This makes
        restaurant conflict-checking reuse `BookingStore`'s existing
        binary "same `resource_key` + time overlap" model unchanged.

        The accepted trade-off: two requests for the same party size at
        the same time are correctly assigned the same table and correctly
        conflict. But a request can also be reported as conflicting with
        an existing booking on "its" table even when a different,
        equally-suitable table happens to be free at that moment -- this
        pack has no way to know that, because table occupancy lives in
        `BookingStore`, not here. This is a real simplification (no live
        table-availability search), not a bug, and is not solved by this
        pack -- doing so would require store-visibility inside the skill
        pack, which the current interfaces do not provide.
    """

    def __init__(
        self,
        tables: Optional[Dict[str, int]] = None,
        working_hours: Optional[Dict[str, str]] = None,
    ) -> None:
        self._tables: Dict[str, int] = (
            dict(tables) if tables is not None else dict(_DEFAULT_TABLES)
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
        """Fields a restaurant BookingRequest must contain.

        Kept in exact sync with the fields `validate()`/`slot_rules()`
        actually read: `party_size`, `customer_name`, `start_time`.
        """
        return [_PARTY_SIZE_FIELD, _CUSTOMER_NAME_FIELD, _START_TIME_FIELD]

    @property
    def working_hours(self) -> Dict:
        """The restaurant's configured daily open/close window.

        Returns the ``{"open": "HH:MM", "close": "HH:MM"}`` dict as
        configured (or the default) -- see the class docstring for the
        shape and semantics.
        """
        return dict(self._working_hours)

    @property
    def tables(self) -> Dict[str, int]:
        """The restaurant's configured table -> seat capacity mapping.

        Returns a copy of the ``{table_id: capacity}`` dict as configured
        (or the default) -- mirrors `working_hours`'s read-only,
        defensive-copy convention.
        """
        return dict(self._tables)

    def _extract_fields(self, request: BookingRequest):
        """Pull the two fields this pack needs, or raise a clear KeyError.

        Deliberately not a violation string: an absent field here means
        the request never should have reached this pack (that's the
        future core/gate's job to catch) -- see class docstring's
        "Precondition / scope note".
        """
        try:
            party_size = request.fields[_PARTY_SIZE_FIELD]
            start_time = request.fields[_START_TIME_FIELD]
        except KeyError as exc:
            raise KeyError(
                f"RestaurantSkillPack requires field {exc}, but it is missing from "
                f"request.fields; missing-field checking belongs to the future "
                f"core/gate, not this sector pack's validate()/slot_rules()"
            ) from exc
        return party_size, start_time

    def _pick_table(self, party_size: int) -> Optional[Tuple[str, int]]:
        """Return `(table_id, capacity)` for the smallest table that fits.

        "Fits" means `capacity >= party_size`. Among fitting tables, picks
        the one with the smallest capacity; ties (multiple tables with the
        same smallest fitting capacity) are broken by lexicographically
        smallest table id -- a stable, documented rule, not a claim that
        one table is otherwise preferable to another.

        Returns:
            `(table_id, capacity)` of the chosen table, or `None` if no
            configured table's capacity is >= `party_size` (the
            over-capacity case; callers turn this into either a validation
            violation or a `ValueError`, see `validate`/`slot_rules`).
        """
        fitting = [
            (capacity, table_id)
            for table_id, capacity in self._tables.items()
            if capacity >= party_size
        ]
        if not fitting:
            return None
        capacity, table_id = min(fitting)
        return table_id, capacity

    def _compute_duration(self, party_size: int) -> int:
        """Flexible duration: base minutes, plus more for larger parties.

        Parties at or below `_DURATION_THRESHOLD_PARTY_SIZE` take the base
        duration; each additional guest beyond the threshold adds
        `_EXTRA_MINUTES_PER_ADDITIONAL_GUEST` minutes. E.g. with this
        pack's defaults (base 60, threshold 2, +15/guest): a party of 2
        takes 60 minutes, a party of 4 takes 90, a party of 8 takes 150.
        This is the "flexible duration" that distinguishes restaurant
        booking from clinic's fixed-per-practitioner duration.
        """
        extra_guests = max(0, party_size - _DURATION_THRESHOLD_PARTY_SIZE)
        return _BASE_DURATION_MINUTES + extra_guests * _EXTRA_MINUTES_PER_ADDITIONAL_GUEST

    def validate(self, request: BookingRequest) -> List[str]:
        """Check sector-specific constraints only.

        Two checks, mirroring `ClinicSkillPack`'s scope boundary:

        1. The party fits in at least one configured table
           ("over-capacity" if not).
        2. The requested start time falls within working hours.

        Does NOT check that `request.fields` contains the required keys
        at all -- see `_extract_fields` / class docstring.

        Returns:
            A list of specific, human-readable violation strings (empty
            if the request satisfies both checks).
        """
        party_size, start_time = self._extract_fields(request)

        violations: List[str] = []

        if self._pick_table(party_size) is None:
            largest_capacity = max(self._tables.values())
            violations.append(
                f"party of {party_size} exceeds largest table capacity of "
                f"{largest_capacity}"
            )

        time_of_day = start_time.time()
        if not (self._open_time <= time_of_day < self._close_time):
            violations.append(
                f"outside working hours: requested {time_of_day.strftime('%H:%M')}, "
                f"hours are {self._working_hours['open']}-{self._working_hours['close']}"
            )

        return violations

    def slot_rules(self, request: BookingRequest) -> SlotInfo:
        """Assign the smallest fitting table and compute flexible duration.

        `capacity` on the returned `SlotInfo` is the request's
        `party_size` (demand), not the assigned table's seat count
        (supply) -- see this task's brief for that resolved design
        question; `capacity` has no functional role in `BookingStore`'s
        conflict logic, so this is a documentation-accuracy choice.
        `resource_key` is `f"table:{table_id}"` -- opaque to everything
        downstream (`BookingStore` only ever compares it for equality), so
        the exact format is this pack's own choice; the `"table:"` prefix
        just keeps it human-readable for debugging, mirroring
        `ClinicSkillPack`'s `"practitioner:"` convention. `start` is the
        request's already-validated `start_time`, passed through
        unchanged via `_extract_fields` rather than re-derived.

        See the class docstring's "Table-assignment design and its known,
        accepted limitation" section for what this deliberately does not
        do (live table-availability search).

        Raises:
            KeyError: if `request.fields` is missing `party_size` or
                `start_time` (see `_extract_fields`).
            ValueError: if no configured table fits `party_size`.
                `validate()` is expected to have already caught this as an
                "over-capacity" violation before `slot_rules()` is called
                on the same request; this is a defensive guard, not this
                method's primary validation path.
        """
        party_size, start_time = self._extract_fields(request)

        picked = self._pick_table(party_size)
        if picked is None:
            largest_capacity = max(self._tables.values())
            raise ValueError(
                f"cannot compute slot rules for party of {party_size}: exceeds "
                f"largest table capacity of {largest_capacity}"
            )
        table_id, _capacity = picked

        return SlotInfo(
            duration_minutes=self._compute_duration(party_size),
            capacity=party_size,
            resource_key=f"table:{table_id}",
            start=start_time,
        )

    def confirmation_template(self) -> str:
        """Return the CONFIRMED-decision message template.

        `.format()`-style placeholders matching this pack's field names,
        for a future renderer (not built in this task) to fill in from
        `request.fields`.
        """
        return (
            "Confirmed: {customer_name}, party of {party_size}, at {start_time}."
        )
