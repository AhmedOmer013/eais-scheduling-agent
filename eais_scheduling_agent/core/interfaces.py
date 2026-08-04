"""Abstract collaborator interfaces the scheduling core depends on.

`SchedulingAgentCore` (see `core.orchestrator`) orchestrates four
collaborators that it never constructs itself: intake, the approval gate,
the booking store, and the audit trail. This module defines all four as
abstract base classes -- the same `abc` pattern T4 used for `SkillPack` --
so that each later task can ship exactly ONE concrete implementation
without any change to the orchestrator or to these definitions.

Why a separate module from `orchestrator.py`:

- These are the project's *published extension points*. Later tasks
  (approval gate, audit trail, persistence, offline intake, LLM intake)
  each import one name from here and implement it. Keeping them out of
  the orchestrator module means an implementer reads ~one screen of
  contract instead of scrolling past orchestration code, and it makes the
  dependency direction obvious: implementations import `core.interfaces`,
  never `core.orchestrator`.
- It keeps `orchestrator.py` about the flow, not the vocabulary.

Sector neutrality: nothing here knows or can know which sector it is
serving. Every method signature is expressed purely in terms of the
generic types from `core.models` (`BookingRequest`, `Decision`,
`AuditRecord`) plus `SlotInfo` from the abstract skill-pack interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Tuple

from eais_scheduling_agent.core.models import AuditRecord, BookingRequest, Decision
from eais_scheduling_agent.skillpacks.base import SlotInfo


@dataclass(frozen=True)
class RuleContext:
    """Everything the approval gate needs to decide, and nothing more.

    This is the `rules` argument of `ApprovalGate.evaluate` (the class
    diagram names the parameter but not its type -- the type is defined
    here). It bundles the two rule sources the core consults, in their
    already-evaluated form, plus the manifest's policy list:

    Attributes:
        missing_fields: Names declared in the skill pack's
            `required_fields` that are absent from `BookingRequest.fields`.
            Computed by the core, because the check is entirely generic:
            it is a set difference over names the skill pack supplied.
            Empty means every declared field was present.
        violations: The skill pack's `validate(request)` output, verbatim
            -- free-text, human-readable, authored by the sector's pack.
            The core neither parses nor interprets these strings; doing so
            would require knowing a sector's vocabulary. Empty means the
            request satisfied every sector rule.
        approval_required_for: The manifest's `approval_required_for`
            list, passed through unchanged. The rule *categories* that
            this sector's operator has declared must escalate rather than
            be auto-confirmed.

    Note for the gate implementer: `violations` are free text while
    `approval_required_for` holds category identifiers. There is
    deliberately no core-side mapping between the two -- see the T6
    report's "Concerns" section. A gate can reasonably treat any non-empty
    `violations` as escalation-worthy and use the strings themselves as
    the `Decision.reason`.

    Stored as tuples rather than lists so the whole bundle is genuinely
    immutable: a gate cannot mutate the core's view of the rules.
    """

    missing_fields: Tuple[str, ...] = ()
    violations: Tuple[str, ...] = ()
    approval_required_for: Tuple[str, ...] = ()


class IntakeService(ABC):
    """Turns free text into a structured `BookingRequest`.

    Implemented once per intake mode (a deterministic offline parser, an
    LLM-backed parser), selected by whoever constructs the core.
    """

    @abstractmethod
    def parse(self, text: str, sector: str) -> BookingRequest:
        """Extract a structured booking request from free text.

        Args:
            text: The raw user input, verbatim.
            sector: The sector tag the request is being handled for. The
                implementation MUST place this value in
                `BookingRequest.sector` unchanged, and SHOULD carry `text`
                into `BookingRequest.raw_text` unchanged -- the core
                relies on both when building the audit record.

                `sector` is passed in (rather than inferred, or stamped on
                by the core afterwards) so that intake fully owns
                `BookingRequest` construction: the type is frozen, and a
                single writer is easier to reason about than
                construct-then-patch. An implementation is also free to
                use it to select an extraction profile, though a
                well-designed intake should not need per-sector code.

        Returns:
            A `BookingRequest` whose `fields` hold whatever structured
            values were extracted. Which field names those are is decided
            between intake and the sector's skill pack; the core only
            compares them against the pack's `required_fields`.
        """
        raise NotImplementedError


class ApprovalGate(ABC):
    """Decides whether a request is auto-confirmed or escalated."""

    @abstractmethod
    def evaluate(
        self, request: BookingRequest, rules: RuleContext, conflict: bool
    ) -> Decision:
        """Return the decision for one request.

        Args:
            request: The parsed request, for context (e.g. to quote a
                field back in the reason text).
            rules: The evaluated rule bundle -- see `RuleContext`.
            conflict: Whether the booking store reported a clash with an
                existing booking. Kept a separate parameter rather than
                folded into `rules` because the class diagram models it
                that way, and because it comes from a different
                collaborator than the rule sources do.

        Returns:
            A `Decision`: `CONFIRMED`, or `PENDING_APPROVAL` with a
            specific, human-readable `reason`. The gate is the only
            component that decides this; the core does not second-guess
            or override the returned decision.
        """
        raise NotImplementedError


class BookingStore(ABC):
    """Holds accepted bookings and answers conflict questions about them."""

    @abstractmethod
    def check_conflict(self, request: BookingRequest, slot: SlotInfo) -> bool:
        """Report whether `request` clashes with an already-stored booking.

        Args:
            request: The request being considered.
            slot: The resource footprint the skill pack computed for this
                request. Passed explicitly because the store cannot derive
                it: duration and capacity are sector rules, and asking the
                store to work them out would give it exactly the sector
                awareness it is not allowed to have.

        Returns:
            True if storing this booking would clash with one already
            held. The core treats the answer as advisory input to the
            gate, never as a decision in itself.
        """
        raise NotImplementedError

    @abstractmethod
    def persist(self, request: BookingRequest, slot: SlotInfo) -> None:
        """Store a confirmed booking, with the footprint it occupies.

        Called by the core only on a `CONFIRMED` decision. Takes `slot`
        for the same reason `check_conflict` does: a later
        `check_conflict` call has to be able to compare against what was
        stored, and the store has no other way to know a booking's
        duration or capacity.
        """
        raise NotImplementedError


class AuditTrail(ABC):
    """Records exactly one entry per processed request."""

    @abstractmethod
    def append(self, record: AuditRecord) -> None:
        """Append one already-built `AuditRecord`.

        The core builds the record and calls this exactly once per
        `handle()` call that reaches a decision -- confirmed or pending
        alike. The implementation's job is durability and format (JSON
        lines, per the plan), not content.
        """
        raise NotImplementedError
