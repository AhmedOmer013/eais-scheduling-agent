"""Scheduling agent core -- orchestration only.

`SchedulingAgentCore.handle(text, sector)` runs one booking request end to
end: load the sector's manifest, resolve its skill pack, parse the text,
evaluate the rules, ask the gate, persist if confirmed, audit, return.

**This module carries zero sector-specific logic**, which is the central
architectural claim of the project. Concretely, that means:

- It never imports a concrete skill pack, and never names one. The
  manifest's `skill_pack` field is an opaque string; the mapping from that
  string to a live `SkillPack` instance is injected by whoever constructs
  the core (a wiring layer or a test), never resolved here.
- It never branches on a sector name. `sector` is used as a lookup key and
  as a value passed through to intake and the audit record -- both of
  which are data movement, not decisions.
- It never interprets a skill pack's rule output. `validate()` returns
  free text the core forwards untouched; deciding what that text means is
  the gate's job, and deciding what the rules *are* is the pack's.

The only sector-shaped work the core does at all is a set difference:
which of the names the pack itself declared in `required_fields` are
absent from the request. That check is generic over any pack's
vocabulary, which is precisely why it belongs here rather than in a pack.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple, Union

from eais_scheduling_agent.core.interfaces import (
    ApprovalGate,
    AuditTrail,
    BookingStore,
    IntakeService,
    RuleContext,
)
from eais_scheduling_agent.core.models import AuditRecord, BookingRequest, Decision
from eais_scheduling_agent.manifests.manifest import (
    ManifestValidationError,
    SectorManifest,
)
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo

#: Manifest file extensions the core will look for, in priority order.
#: Mirrors exactly what `SectorManifest.load` accepts; the core does not
#: invent a format of its own.
_MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")

#: The `Decision.status` value that means "no human approval needed".
#: Compared against, not constructed -- the vocabulary belongs to
#: `Decision` (see `core.models`).
_CONFIRMED = "CONFIRMED"

#: `AuditRecord.approval_status` for each decision status. A confirmed
#: booking was never sent for approval, so its approval status is
#: "not_required" rather than "approved" -- nobody approved anything.
#: An escalated one is "pending" until some future workflow resolves it.
_APPROVAL_STATUS = {
    "CONFIRMED": "not_required",
    "PENDING_APPROVAL": "pending",
}


class OrchestrationError(Exception):
    """Base class for configuration faults detected while handling a request.

    All three subclasses signal a *configuration* problem (a sector that
    is not set up, or is set up incorrectly), never a problem with the
    booking itself. A bad booking produces a `PENDING_APPROVAL` decision;
    a bad configuration raises. That split is deliberate: a caller can
    always show a `Decision` to a user, but "this sector does not exist"
    is not something a user can act on, and silently returning
    `PENDING_APPROVAL` for it would put an unactionable record in the
    audit trail and hide a deployment mistake.

    Nothing is persisted or audited when one of these is raised: no
    request was ever parsed, so there is no processed request to record.
    """


class UnknownSectorError(OrchestrationError):
    """No manifest file exists for the requested sector.

    Also raised when the sector name could not be used to look one up at
    all -- see `SchedulingAgentCore._load_manifest`.
    """


class InvalidManifestError(OrchestrationError):
    """The sector's manifest file exists but failed validation.

    Wraps `manifests.manifest.ManifestValidationError` so that every way a
    sector can be unusable is reachable through one `except
    OrchestrationError` clause.
    """


class SectorDisabledError(OrchestrationError):
    """The sector's manifest exists but declares `enabled: false`."""


class UnknownSkillPackError(OrchestrationError):
    """The manifest names a skill pack the injected resolver has no entry for."""


class SchedulingAgentCore:
    """Orchestrates one booking request across manifest, pack, and collaborators.

    Args:
        manifest_dir: Directory holding one manifest file per sector,
            named `<sector>.yaml` (or `.yml` / `.json`). Sector lookup is
            by filename, which is what makes adding a sector a
            drop-in-a-file operation with no wiring change anywhere --
            matching the extension-point contract in ARCHITECTURE.md.
        skill_packs: Maps a manifest's `skill_pack` identifier string to a
            live `SkillPack` instance. **This mapping is how the core
            stays sector-agnostic**: the core reads a string out of a
            manifest and looks it up: it never imports, constructs, or
            names a concrete pack. Whoever builds this mapping (a wiring
            layer, or a test) is allowed to name sectors, because it lives
            outside this package.

            Typed as `Mapping`, not `dict`: any object supporting `in` and
            `[]` works, so a caller wanting lazy or dynamic resolution can
            supply a custom `Mapping` implementation that constructs packs
            on demand. A plain dict remains the simple default.
        intake: Turns free text into a `BookingRequest`.
        gate: Decides confirmed vs. escalated.
        store: Answers conflict questions and holds confirmed bookings.
        audit: Receives exactly one record per handled request.

    The core trusts its four collaborators to honour their interface
    contracts and does not type-police their return values -- consistently
    for all four. A collaborator that breaks its contract surfaces as an
    ordinary Python error at the point of misuse rather than as a
    core-invented error class.
    """

    def __init__(
        self,
        manifest_dir: Union[str, Path],
        skill_packs: Mapping[str, SkillPack],
        intake: IntakeService,
        gate: ApprovalGate,
        store: BookingStore,
        audit: AuditTrail,
    ) -> None:
        self._manifest_dir = Path(manifest_dir)
        self._skill_packs = skill_packs
        self._intake = intake
        self._gate = gate
        self._store = store
        self._audit = audit

    # -- orchestration ---------------------------------------------------

    def handle(self, text: str, sector: str) -> Decision:
        """Run one booking request end to end and return its decision.

        Args:
            text: Raw free-text input from the user.
            sector: Which sector's manifest and rules to handle it under.

        Returns:
            The `Decision` produced by the approval gate.

        Raises:
            UnknownSectorError: No manifest file for `sector`, or `sector`
                is not usable as a manifest file name.
            InvalidManifestError: The manifest file exists but failed
                validation.
            SectorDisabledError: The sector's manifest declares
                `enabled: false`.
            UnknownSkillPackError: The manifest's `skill_pack` string is
                absent from the injected mapping.

        All four are `OrchestrationError` subclasses, so a caller can
        catch that one type to cover every configuration fault. Anything
        else that escapes comes from a collaborator or a skill pack
        failing its own contract, and is deliberately not translated --
        the core cannot say anything truthful about what went wrong
        inside someone else's implementation.

        Audit guarantee: exactly one record is written for every request
        that reaches a decision, including when persisting it afterwards
        fails. A failure *before* the gate returns leaves no record,
        because there is no decision to record.
        """
        # 1. Manifest for this sector, and the enabled check.
        manifest = self._load_manifest(sector)
        if not manifest.enabled:
            raise SectorDisabledError(
                f"sector {sector!r} is present but disabled in its manifest"
            )

        # 2. Resolve the manifest's skill-pack identifier to an instance.
        skill_pack = self._resolve_skill_pack(manifest.skill_pack)

        # 3. Free text -> structured request. Intake owns construction;
        #    it is handed `sector` so the frozen request is right first
        #    time rather than being patched afterwards.
        request = self._intake.parse(text, sector)

        # 4. Rule evaluation, in two parts.
        #    (a) Generic: which fields the pack declared are absent.
        missing_fields = self._missing_fields(skill_pack, request)
        #    (b) Sector-specific: delegated wholesale to the pack.
        #        Skipped when fields are missing -- a pack is entitled to
        #        assume its required fields are present (that precondition
        #        is part of the skill-pack contract), so calling it with an
        #        incomplete request could raise instead of returning
        #        violations. The missing-field findings still reach the
        #        gate, so nothing is lost from the decision.
        violations: Tuple[str, ...] = ()
        if not missing_fields:
            violations = tuple(skill_pack.validate(request))

        # 5. Conflict check, which needs the footprint only the pack can
        #    compute. Both are skipped when the request already failed the
        #    checks above, for the same precondition reason: `slot_rules`
        #    is only meaningful for a request its pack accepts.
        #
        #    `conflict_checked` is recorded here, at the point the check
        #    is actually run or skipped, rather than being inferred later
        #    from `slot is not None`: step 7 may compute a slot after the
        #    fact, and inferring from it there would make the audit record
        #    claim a conflict check that never happened.
        slot: Optional[SlotInfo] = None
        conflict = False
        conflict_checked = False
        if not missing_fields and not violations:
            slot = skill_pack.slot_rules(request)
            conflict = self._store.check_conflict(request, slot)
            conflict_checked = True

        # 6. The gate, and only the gate, decides.
        rules = RuleContext(
            missing_fields=missing_fields,
            violations=violations,
            approval_required_for=tuple(manifest.approval_required_for),
        )
        decision = self._gate.evaluate(request, rules, conflict)

        # 7. Persist confirmed bookings only, and 8. record the outcome
        #    exactly once per handled request, on every path.
        #
        #    The audit append sits in a `finally` because both statements
        #    inside the `try` can raise: `slot_rules` refuses a request
        #    its own pack rejected, and a real store can fail to write.
        #    Losing the audit record precisely when something went wrong
        #    is the worst time to lose it, so the record is written and
        #    the exception is then allowed to propagate. The record
        #    describes what was *decided* (which is settled by this
        #    point); it does not claim the booking was stored.
        try:
            if decision.status == _CONFIRMED:
                if slot is None:
                    # The gate confirmed a request whose rules did not
                    # pass, so no footprint was computed above. Compute
                    # one now rather than persisting a booking with no
                    # footprint the store could later compare against; if
                    # the pack refuses, that error is the honest outcome.
                    slot = skill_pack.slot_rules(request)
                self._store.persist(request, slot)
        finally:
            self._audit.append(
                self._build_audit_record(
                    request=request,
                    manifest=manifest,
                    decision=decision,
                    missing_fields=missing_fields,
                    violations=violations,
                    conflict_checked=conflict_checked,
                    conflict=conflict,
                )
            )

        # 9. Hand the decision back untouched.
        return decision

    # -- helpers ---------------------------------------------------------

    def _load_manifest(self, sector: str) -> SectorManifest:
        """Find and load `<manifest_dir>/<sector>.<supported suffix>`.

        `sector` becomes part of a filesystem path, and it will eventually
        arrive from a CLI argument or an HTTP parameter, so it is checked
        first: it has to be usable as a bare filename. Anything with a
        path separator, a parent-directory hop, or a drive prefix is
        rejected before it can walk out of `manifest_dir`.
        """
        unusable_as_filename = (
            not sector
            or sector != Path(sector).name
            or "/" in sector
            or "\\" in sector
        )
        if unusable_as_filename:
            raise UnknownSectorError(
                f"sector {sector!r} is not a usable manifest name: a sector "
                f"must be a plain file name, without path separators or "
                f"parent-directory references"
            )

        for suffix in _MANIFEST_SUFFIXES:
            candidate = self._manifest_dir / f"{sector}{suffix}"
            if candidate.is_file():
                try:
                    return SectorManifest.load(str(candidate))
                except ManifestValidationError as exc:
                    # Re-raised as an OrchestrationError so a caller can
                    # catch every "this sector is not usable" failure with
                    # one except clause. A manifest typo is the most
                    # likely deployment fault in the system; it should not
                    # be the one that escapes as a raw traceback.
                    raise InvalidManifestError(
                        f"manifest for sector {sector!r} at {candidate} is "
                        f"invalid: {exc}"
                    ) from exc
        raise UnknownSectorError(
            f"no manifest for sector {sector!r} in {self._manifest_dir}: "
            f"expected one of "
            f"{', '.join(f'{sector}{s}' for s in _MANIFEST_SUFFIXES)}"
        )

    def _resolve_skill_pack(self, identifier: str) -> SkillPack:
        """Look the manifest's skill-pack identifier up in the injected map.

        A single subscript, deliberately: a `Mapping` implementation that
        builds packs on demand in `__getitem__` would do that work twice
        if this asked `in` first and then subscripted.
        """
        try:
            return self._skill_packs[identifier]
        except KeyError as exc:
            raise UnknownSkillPackError(
                f"manifest requires skill pack {identifier!r}, which was not "
                f"supplied to SchedulingAgentCore"
            ) from exc

    @staticmethod
    def _missing_fields(
        skill_pack: SkillPack, request: BookingRequest
    ) -> Tuple[str, ...]:
        """Names the pack declared required that the request does not carry.

        Key presence only: a key present with a `None` value counts as
        supplied, and is left for the skill pack to judge on its value.
        Intake is contractually required not to emit such placeholders
        (see `IntakeService.parse`); second-guessing what an absent-ish
        value means would be the core deciding a sector question.

        Order follows the pack's own `required_fields` declaration, so the
        result -- and therefore the audit record built from it -- is
        deterministic.
        """
        return tuple(
            name for name in skill_pack.required_fields if name not in request.fields
        )

    @staticmethod
    def _build_audit_record(
        request: BookingRequest,
        manifest: SectorManifest,
        decision: Decision,
        missing_fields: Sequence[str],
        violations: Sequence[str],
        conflict_checked: bool,
        conflict: bool,
    ) -> AuditRecord:
        """Assemble the single record describing how this request was handled.

        `rules_evaluated` records every check the core ran and how it
        came out -- including checks that were deliberately skipped, since
        "this was not checked" is exactly the kind of thing an audit
        reader needs to be able to see. It is always three entries, in a
        fixed order, so the trail is uniform across requests and sectors.
        """
        rules_evaluated: List[str] = [
            "required_fields: "
            + (f"missing {', '.join(missing_fields)}" if missing_fields else "ok"),
            "skill_pack_validation: "
            + (
                "skipped (required fields missing)"
                if missing_fields
                else f"violations: {'; '.join(violations)}"
                if violations
                else "ok"
            ),
            "conflict_check: "
            + (
                "skipped (rules not satisfied)"
                if not conflict_checked
                else "conflict"
                if conflict
                else "none"
            ),
        ]

        return AuditRecord(
            input=request.raw_text,
            skill_pack=manifest.skill_pack,
            intent=dict(request.fields),
            rules_evaluated=rules_evaluated,
            decision=decision.status,
            approval_status=_APPROVAL_STATUS[decision.status],
            timestamp=datetime.now(timezone.utc),
        )
