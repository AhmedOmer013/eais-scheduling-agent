"""In-memory booking store with same-run conflict detection (T9).

`BookingStore` (see `core.interfaces`) is the core's only source of
conflict information: `check_conflict` runs before the gate decides,
`persist` runs only after a `CONFIRMED` decision (see that interface's
docstrings). Per T6's report and the T9 brief, this implementation
depends only on the generic `BookingRequest` / `SlotInfo` shapes -- it
never reads a sector-defined field name out of `request.fields`, and it
never interprets `SlotInfo.resource_key` beyond comparing it for
equality. That is what keeps it sector-agnostic while still able to
answer "does this new booking clash with one already held."

Storage is a plain in-memory list, not SQLite. PLAN.md's done-when says
"in-memory or SQLite" and scopes this task to "same-run conflict
detection" -- i.e. process-lifetime persistence is the actual
requirement, not durability across runs. A list of persisted slots is
sufficient for that and adds no dependency or schema for no functional
gain.
"""

from datetime import datetime, timedelta
from typing import List, NamedTuple

from eais_scheduling_agent.core.interfaces import BookingStore
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.skillpacks.base import SlotInfo


class _PersistedBooking(NamedTuple):
    """The three fields conflict detection actually needs, kept apart from
    the full `SlotInfo` (and from `request`) so it's clear this store
    depends on exactly this much and nothing else.
    """

    resource_key: str
    start: datetime
    duration_minutes: int


def _intervals_overlap(
    start_a: datetime, duration_a: int, start_b: datetime, duration_b: int
) -> bool:
    """Half-open interval overlap: [start_a, start_a+duration_a) vs.
    [start_b, start_b+duration_b).

    Overlap iff `start_a < end_b AND start_b < end_a`. Strict `<` on
    both sides (rather than `<=`) is what makes touching-but-not-
    overlapping intervals -- one ends exactly when the other starts --
    correctly NOT overlap, the same open-inclusive/close-exclusive
    convention a working-hours check would use.
    """
    end_a = start_a + timedelta(minutes=duration_a)
    end_b = start_b + timedelta(minutes=duration_b)
    return start_a < end_b and start_b < end_a


class InMemoryBookingStore(BookingStore):
    """Holds confirmed bookings for the lifetime of the process.

    Conflict rule: two bookings conflict only if they share the same
    `SlotInfo.resource_key` AND their `[start, start + duration_minutes)`
    intervals overlap (half-open, per `_intervals_overlap`). Different
    resources never conflict, regardless of timing.

    `check_conflict` only ever compares against bookings that have
    already been `persist`-ed -- never against every request this store
    has been asked about. That matches T6's call pattern: `persist` is
    only called by the core on a `CONFIRMED` decision.
    """

    def __init__(self) -> None:
        self._bookings: List[_PersistedBooking] = []

    def check_conflict(self, request: BookingRequest, slot: SlotInfo) -> bool:
        """Return True if `slot` overlaps any already-persisted booking
        that shares its `resource_key`.

        `request` is accepted per the `BookingStore` interface but not
        otherwise used: every persisted record and every comparison here
        is keyed purely on `SlotInfo` fields, which is what keeps this
        store from needing to know anything about a sector's request
        shape.
        """
        for booking in self._bookings:
            if booking.resource_key != slot.resource_key:
                continue
            if _intervals_overlap(
                booking.start, booking.duration_minutes, slot.start, slot.duration_minutes
            ):
                return True
        return False

    def persist(self, request: BookingRequest, slot: SlotInfo) -> None:
        """Record `slot` so future `check_conflict` calls see it.

        `request` is accepted per the `BookingStore` interface but not
        otherwise used, for the same reason as `check_conflict`.
        """
        self._bookings.append(
            _PersistedBooking(
                resource_key=slot.resource_key,
                start=slot.start,
                duration_minutes=slot.duration_minutes,
            )
        )
