"""Tests for InMemoryBookingStore (T9), in isolation.

Constructs SlotInfo directly -- no real ClinicSkillPack or core involved,
per the T9 brief. `request` is accepted by both `BookingStore` methods
but never inspected by this implementation, so a single throwaway
BookingRequest is reused across all cases.
"""

from datetime import datetime, timedelta

from eais_scheduling_agent.core.interfaces import BookingStore
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.core.store import InMemoryBookingStore
from eais_scheduling_agent.skillpacks.base import SlotInfo

REQUEST = BookingRequest(sector="clinic", fields={}, raw_text="irrelevant")


def make_slot(resource_key="practitioner:Dr. A", start=None, duration_minutes=30):
    if start is None:
        start = datetime(2026, 8, 4, 10, 0)
    return SlotInfo(
        duration_minutes=duration_minutes,
        capacity=1,
        resource_key=resource_key,
        start=start,
    )


class TestInMemoryBookingStoreSatisfiesInterface:
    def test_is_a_booking_store(self):
        store = InMemoryBookingStore()
        assert isinstance(store, BookingStore)


class TestNoPersistedBookings:
    def test_check_conflict_returns_false_when_store_is_empty(self):
        store = InMemoryBookingStore()
        candidate = make_slot()

        assert store.check_conflict(REQUEST, candidate) is False


class TestOverlappingSameResource:
    def test_persisted_then_overlapping_candidate_conflicts(self):
        store = InMemoryBookingStore()
        persisted = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)
        store.persist(REQUEST, persisted)

        # Overlaps: [10:00, 10:30) vs. [10:15, 10:45)
        candidate = make_slot(start=datetime(2026, 8, 4, 10, 15), duration_minutes=30)

        assert store.check_conflict(REQUEST, candidate) is True


class TestNonOverlappingSameResource:
    def test_candidate_entirely_before_persisted_does_not_conflict(self):
        store = InMemoryBookingStore()
        persisted = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)
        store.persist(REQUEST, persisted)

        # [8:00, 8:30) is well before [10:00, 10:30)
        candidate = make_slot(start=datetime(2026, 8, 4, 8, 0), duration_minutes=30)

        assert store.check_conflict(REQUEST, candidate) is False

    def test_candidate_entirely_after_persisted_does_not_conflict(self):
        store = InMemoryBookingStore()
        persisted = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)
        store.persist(REQUEST, persisted)

        # [14:00, 14:30) is well after [10:00, 10:30)
        candidate = make_slot(start=datetime(2026, 8, 4, 14, 0), duration_minutes=30)

        assert store.check_conflict(REQUEST, candidate) is False


class TestAdjacentBoundary:
    """Touching-but-not-overlapping intervals: half-open, so no conflict."""

    def test_candidate_starts_exactly_when_persisted_ends(self):
        store = InMemoryBookingStore()
        # Persisted: [10:00, 10:30)
        persisted = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)
        store.persist(REQUEST, persisted)

        # Candidate starts exactly at persisted's end: [10:30, 11:00)
        candidate = make_slot(start=datetime(2026, 8, 4, 10, 30), duration_minutes=30)

        assert store.check_conflict(REQUEST, candidate) is False

    def test_persisted_starts_exactly_when_candidate_ends(self):
        store = InMemoryBookingStore()
        # Persisted: [10:30, 11:00)
        persisted = make_slot(start=datetime(2026, 8, 4, 10, 30), duration_minutes=30)
        store.persist(REQUEST, persisted)

        # Candidate ends exactly at persisted's start: [10:00, 10:30)
        candidate = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)

        assert store.check_conflict(REQUEST, candidate) is False

    def test_overlapping_by_one_minute_does_conflict(self):
        """Shift the adjacent case by one minute so they genuinely overlap."""
        store = InMemoryBookingStore()
        # Persisted: [10:00, 10:30)
        persisted = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)
        store.persist(REQUEST, persisted)

        # Candidate starts one minute before persisted ends: [10:29, 10:59)
        candidate = make_slot(
            start=datetime(2026, 8, 4, 10, 30) - timedelta(minutes=1), duration_minutes=30
        )

        assert store.check_conflict(REQUEST, candidate) is True


class TestDifferentResourceKey:
    def test_identical_interval_different_resource_does_not_conflict(self):
        store = InMemoryBookingStore()
        persisted = make_slot(
            resource_key="practitioner:Dr. A",
            start=datetime(2026, 8, 4, 10, 0),
            duration_minutes=30,
        )
        store.persist(REQUEST, persisted)

        # Same interval, different resource_key.
        candidate = make_slot(
            resource_key="practitioner:Dr. B",
            start=datetime(2026, 8, 4, 10, 0),
            duration_minutes=30,
        )

        assert store.check_conflict(REQUEST, candidate) is False


class TestChecksAllPersistedBookings:
    def test_conflict_with_only_one_of_two_persisted_bookings_is_detected(self):
        store = InMemoryBookingStore()

        # Two non-conflicting persisted bookings for the same resource.
        booking_1 = make_slot(start=datetime(2026, 8, 4, 9, 0), duration_minutes=30)
        booking_2 = make_slot(start=datetime(2026, 8, 4, 14, 0), duration_minutes=30)
        store.persist(REQUEST, booking_1)
        store.persist(REQUEST, booking_2)

        # Candidate conflicts only with booking_2: [14:15, 14:45) overlaps
        # [14:00, 14:30) but not [9:00, 9:30).
        candidate = make_slot(start=datetime(2026, 8, 4, 14, 15), duration_minutes=30)

        assert store.check_conflict(REQUEST, candidate) is True


class TestPersist:
    def test_persist_does_not_raise(self):
        store = InMemoryBookingStore()
        slot = make_slot()

        store.persist(REQUEST, slot)  # should not raise

    def test_persisted_booking_is_visible_to_later_check_conflict(self):
        store = InMemoryBookingStore()
        slot = make_slot(start=datetime(2026, 8, 4, 10, 0), duration_minutes=30)

        assert store.check_conflict(REQUEST, slot) is False
        store.persist(REQUEST, slot)
        assert store.check_conflict(REQUEST, slot) is True
