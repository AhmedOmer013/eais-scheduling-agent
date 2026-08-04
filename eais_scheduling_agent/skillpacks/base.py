"""Abstract skill-pack interface.

Defines the contract that every sector's skill pack (the clinic pack in
T5, the restaurant pack in T10) must implement, and that
`SchedulingAgentCore` (T6) depends on -- *only* on this abstract shape,
never on a concrete subclass.

This module is pure interface: no sector-specific logic lives here.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List

from eais_scheduling_agent.core.models import BookingRequest


@dataclass(frozen=True)
class SlotInfo:
    """The resource footprint a single booking occupies.

    `slot_rules(request)` returns one of these to describe how much of a
    sector's resource a booking consumes. Two known sectors have to be
    expressible without lying about their data:

    - A fixed-length-per-practitioner sector (clinic, T5): a practitioner
      sees exactly one patient for a duration fixed by that practitioner's
      schedule. There is no notion of splitting or sharing the slot.
    - A flexible-duration-by-capacity sector (restaurant, T10): a table's
      duration and the capacity it must supply both depend on the party
      size (and sector-specific rules), and a booking legitimately
      consumes more than one "unit" of the resource.

    `duration_minutes` and `capacity` together cover both: duration
    captures the "fixed slot length" axis, capacity captures the
    "how much of the resource" axis. A clinic pack reports a constant
    `capacity=1` (one practitioner, one patient) -- that is not a
    placeholder or a lie, it is the literal capacity of a clinic slot.
    A restaurant pack computes both fields from party size / table rules.

    Deliberately excluded: anything sector-specific like a table id,
    practitioner id, or start/end timestamps. Those belong in the
    concrete skill packs (or in `BookingRequest.fields`), not in the
    shared interface type -- adding them here would be speculative, since
    neither T5 nor T10 has a known need for them yet.

    Attributes:
        duration_minutes: How long the booking occupies its resource, in
            minutes. Fixed per practitioner for a clinic; computed from
            party size (or other sector rules) for a restaurant.
        capacity: How many units of the resource the booking consumes.
            Always 1 for a clinic slot (one practitioner, one patient).
            The party size (or the table size needed to seat it) for a
            restaurant slot.
    """

    duration_minutes: int
    capacity: int


class SkillPack(ABC):
    """Abstract sector skill pack.

    A skill pack packages everything sector-specific the scheduling core
    needs so that the core never depends on which sector it is talking
    to. `SchedulingAgentCore` (T6) is written against this interface only;
    it must never branch on, or import, a concrete subclass.

    `required_fields` and `working_hours` are declared as abstract
    *properties* rather than abstract methods, because they are data a
    caller reads (`pack.required_fields`), not behavior a caller invokes.
    Concrete subclasses may satisfy them however is convenient -- a plain
    class attribute, an instance attribute set in `__init__`, or an actual
    `@property` -- `abc` only requires that the name be overridden
    somewhere in the subclass; the override does not itself have to stay
    a property. This keeps trivial cases (a fixed list, a fixed dict)
    simple while still allowing a subclass to compute either dynamically
    if a future sector needs to.
    """

    @property
    @abstractmethod
    def required_fields(self) -> List[str]:
        """Fields a BookingRequest must contain for this sector.

        E.g. clinic: ``["practitioner", "patient_name"]``; restaurant:
        ``["party_size"]``.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def working_hours(self) -> Dict:
        """When bookings are permitted at all, for this sector."""
        raise NotImplementedError

    @abstractmethod
    def validate(self, request: BookingRequest) -> List[str]:
        """Check `request` against this sector's constraints.

        Args:
            request: The booking request to validate.

        Returns:
            A list of violation strings/reasons. An empty list means no
            violations. This is what the approval gate (T7) calls to
            help decide CONFIRMED vs. PENDING_APPROVAL.
        """
        raise NotImplementedError

    @abstractmethod
    def slot_rules(self, request: BookingRequest) -> SlotInfo:
        """Compute the duration/capacity `request` occupies.

        Args:
            request: The booking request to compute slot rules for.

        Returns:
            A SlotInfo describing the resource footprint of the booking
            (fixed-length-per-practitioner for a clinic, flexible
            duration-by-party-size for a restaurant).
        """
        raise NotImplementedError

    @abstractmethod
    def confirmation_template(self) -> str:
        """Return the message template rendered on a CONFIRMED decision."""
        raise NotImplementedError
