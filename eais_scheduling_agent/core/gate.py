"""The concrete approval gate (T7).

`ApprovalGate` (see `core.interfaces`) is the one place that turns the
core's already-evaluated rule signals into a `Decision`. Per that
interface's docstring, there is deliberately no core-side mapping between
`RuleContext.violations` (free text, authored by a sector's skill pack)
and `RuleContext.approval_required_for` (the manifest's category list):
this gate treats any non-empty rule signal as escalation-worthy,
unconditionally, without consulting `approval_required_for` at all.

Nothing here is sector-specific: this module reasons only about the
generic `RuleContext` shape and the `conflict` flag, never about what a
particular sector's fields or violation strings mean.
"""

from eais_scheduling_agent.core.interfaces import ApprovalGate, RuleContext
from eais_scheduling_agent.core.models import BookingRequest, Decision

#: `Decision.status` values, mirrored from `core.models.Decision` so this
#: module never spells the strings out more than once.
_CONFIRMED = "CONFIRMED"
_PENDING_APPROVAL = "PENDING_APPROVAL"

#: Separator used to join multiple violation strings into one reason.
_VIOLATION_JOIN = "; "


class StandardApprovalGate(ApprovalGate):
    """Escalates on any missing field, any violation, or a reported conflict.

    Decision logic, applied in this priority order (first match wins),
    per the T7 brief -- defensive rather than assuming the three signals
    are mutually exclusive, even though the real core only ever populates
    one of them at a time (see `core.orchestrator`'s precondition-skipping
    design):

    1. `rules.missing_fields` non-empty -> `PENDING_APPROVAL`, naming the
       missing field(s).
    2. `rules.violations` non-empty -> `PENDING_APPROVAL`, using the
       violation text itself (already specific and human-readable, per
       the skill pack's own design) as the reason. Multiple violations
       are joined into one reason string.
    3. `conflict` is True -> `PENDING_APPROVAL`, describing a
       double-booking/conflict.
    4. None of the above -> `CONFIRMED`.

    `rules.approval_required_for` is intentionally not consulted: per
    `RuleContext`'s docstring, there is no core-side mapping from a
    sector's free-text violations to the manifest's category identifiers,
    so this gate escalates on any non-empty signal rather than gating
    escalation on category membership.
    """

    def evaluate(
        self, request: BookingRequest, rules: RuleContext, conflict: bool
    ) -> Decision:
        """Return `CONFIRMED` or `PENDING_APPROVAL` with a specific reason.

        See the class docstring for the priority order. `request` is
        accepted per the `ApprovalGate` interface but not otherwise used:
        every reason this gate produces is built entirely from `rules`
        and `conflict`.
        """
        if rules.missing_fields:
            fields = ", ".join(rules.missing_fields)
            return Decision(
                status=_PENDING_APPROVAL,
                reason=f"missing required field(s): {fields}",
            )

        if rules.violations:
            reason = _VIOLATION_JOIN.join(rules.violations)
            return Decision(status=_PENDING_APPROVAL, reason=reason)

        if conflict:
            return Decision(
                status=_PENDING_APPROVAL,
                reason="requested slot conflicts with an existing booking",
            )

        return Decision(status=_CONFIRMED)
