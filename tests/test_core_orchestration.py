"""Tests for SchedulingAgentCore (T6) -- orchestration only.

Wiring under test: trivial, throwaway fakes for the four collaborator
interfaces (intake, gate, store, audit -- defined here, never under
`core/`, same pattern T4 used for its fake skill pack) combined with the
**real** `SectorManifest` loader and the **real** `ClinicSkillPack`. That
combination is the point: it proves a real sector runs end to end through
a core that has never heard of it, rather than proving fakes can talk to
fakes.

Naming a sector is legal *here*, in the wiring layer, and only here --
`tests/` is explicitly outside the FR1 boundary. `TestCoreHasNoSectorNames`
at the bottom of this file enforces that boundary on `core/` itself.
"""

import re
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest

from eais_scheduling_agent.core import orchestrator as orchestrator_module
from eais_scheduling_agent.core.interfaces import (
    ApprovalGate,
    AuditTrail,
    BookingStore,
    IntakeService,
    RuleContext,
)
from eais_scheduling_agent.core.models import AuditRecord, BookingRequest, Decision
from eais_scheduling_agent.core.orchestrator import (
    InvalidManifestError,
    OrchestrationError,
    SchedulingAgentCore,
    SectorDisabledError,
    UnknownSectorError,
    UnknownSkillPackError,
)
from eais_scheduling_agent.manifests.manifest import ManifestValidationError
from eais_scheduling_agent.skillpacks.base import SkillPack, SlotInfo
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack

SECTOR_FIXTURES = Path(__file__).parent / "fixtures" / "sectors"
SECTOR = "clinic"
PACK_ID = "clinic_v1"


# ---------------------------------------------------------------------------
# Throwaway fakes -- one per interface, no behaviour beyond recording calls
# ---------------------------------------------------------------------------


class FakeIntake(IntakeService):
    """Returns preset fields; records the (text, sector) it was called with."""

    def __init__(self, fields):
        self.fields = fields
        self.calls = []

    def parse(self, text, sector):
        self.calls.append((text, sector))
        return BookingRequest(sector=sector, fields=dict(self.fields), raw_text=text)


class FakeGate(ApprovalGate):
    """Returns a scripted Decision; records every (request, rules, conflict)."""

    def __init__(self, decision=None):
        self.decision = decision or Decision(status="CONFIRMED")
        self.calls = []

    def evaluate(self, request, rules, conflict):
        self.calls.append((request, rules, conflict))
        return self.decision


class FakeStore(BookingStore):
    """Reports a scripted conflict answer; records conflict/persist calls."""

    def __init__(self, conflict=False):
        self.conflict = conflict
        self.conflict_calls = []
        self.persisted = []

    def check_conflict(self, request, slot):
        self.conflict_calls.append((request, slot))
        return self.conflict

    def persist(self, request, slot):
        self.persisted.append((request, slot))


class FakeAudit(AuditTrail):
    """Collects appended records in memory."""

    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)


VALID_FIELDS = {
    "practitioner": "Dr. A",
    "patient_name": "Sam Patel",
    "start_time": datetime(2026, 8, 5, 10, 30),
}
OUT_OF_HOURS_FIELDS = dict(VALID_FIELDS, start_time=datetime(2026, 8, 5, 20, 0))
TEXT = "book Sam Patel with Dr. A tomorrow at 10:30"


def build_core(fields=None, decision=None, conflict=False, sector_dir=None):
    """Wire a core from fakes + the real manifest loader + the real pack."""
    intake = FakeIntake(VALID_FIELDS if fields is None else fields)
    gate = FakeGate(decision)
    store = FakeStore(conflict)
    audit = FakeAudit()
    core = SchedulingAgentCore(
        manifest_dir=sector_dir or SECTOR_FIXTURES,
        skill_packs={PACK_ID: ClinicSkillPack()},
        intake=intake,
        gate=gate,
        store=store,
        audit=audit,
    )
    return core, intake, gate, store, audit


# ---------------------------------------------------------------------------


class TestConfirmedPathEndToEnd:
    """A real sector runs end to end through the core and is confirmed."""

    def test_returns_the_gates_decision(self):
        core, _, _, _, _ = build_core()

        decision = core.handle(TEXT, SECTOR)

        assert isinstance(decision, Decision)
        assert decision.status == "CONFIRMED"

    def test_intake_receives_text_and_sector(self):
        core, intake, _, _, _ = build_core()

        core.handle(TEXT, SECTOR)

        assert intake.calls == [(TEXT, SECTOR)]

    def test_persists_exactly_once_with_the_packs_slot(self):
        core, _, _, store, _ = build_core()

        core.handle(TEXT, SECTOR)

        assert len(store.persisted) == 1
        request, slot = store.persisted[0]
        assert request.sector == SECTOR
        # Real ClinicSkillPack: "Dr. A" is a 30-minute, capacity-1 slot.
        assert slot == SlotInfo(
            duration_minutes=30,
            capacity=1,
            resource_key="practitioner:Dr. A",
            start=VALID_FIELDS["start_time"],
        )

    def test_conflict_check_runs_before_the_gate_with_the_same_slot(self):
        core, _, gate, store, _ = build_core()

        core.handle(TEXT, SECTOR)

        assert len(store.conflict_calls) == 1
        assert store.conflict_calls[0][1] == SlotInfo(
            duration_minutes=30,
            capacity=1,
            resource_key="practitioner:Dr. A",
            start=VALID_FIELDS["start_time"],
        )
        assert gate.calls[0][2] is False

    def test_gate_sees_clean_rules_from_the_real_pack_and_manifest(self):
        core, _, gate, _, _ = build_core()

        core.handle(TEXT, SECTOR)

        (_request, rules, _conflict) = gate.calls[0]
        assert isinstance(rules, RuleContext)
        assert rules.missing_fields == ()
        assert rules.violations == ()
        # Passed through from the real manifest file, unchanged.
        assert rules.approval_required_for == (
            "outside_working_hours",
            "double_booking",
            "missing_required_field",
        )

    def test_appends_exactly_one_audit_record(self):
        core, _, _, _, audit = build_core()

        core.handle(TEXT, SECTOR)

        assert len(audit.records) == 1
        assert isinstance(audit.records[0], AuditRecord)


class TestPendingApprovalPath:
    """When the gate escalates, nothing is persisted but a record is written."""

    def test_returns_the_pending_decision_with_reason(self):
        core, _, _, _, _ = build_core(
            decision=Decision(status="PENDING_APPROVAL", reason="needs a human")
        )

        decision = core.handle(TEXT, SECTOR)

        assert decision.status == "PENDING_APPROVAL"
        assert decision.reason == "needs a human"

    def test_does_not_persist(self):
        core, _, _, store, _ = build_core(
            decision=Decision(status="PENDING_APPROVAL", reason="needs a human")
        )

        core.handle(TEXT, SECTOR)

        assert store.persisted == []

    def test_still_appends_exactly_one_audit_record(self):
        core, _, _, _, audit = build_core(
            decision=Decision(status="PENDING_APPROVAL", reason="needs a human")
        )

        core.handle(TEXT, SECTOR)

        assert len(audit.records) == 1
        assert audit.records[0].decision == "PENDING_APPROVAL"
        assert audit.records[0].approval_status == "pending"


class TestConflictIsReportedToTheGate:
    """A store-reported conflict reaches the gate as its third argument."""

    def test_conflict_true_is_forwarded(self):
        core, _, gate, _, _ = build_core(conflict=True)

        core.handle(TEXT, SECTOR)

        assert gate.calls[0][2] is True


class TestSectorRuleViolationsReachTheGate:
    """The real pack's violations are forwarded verbatim, uninterpreted."""

    def test_out_of_hours_violation_is_passed_through(self):
        core, _, gate, store, _ = build_core(
            fields=OUT_OF_HOURS_FIELDS,
            decision=Decision(status="PENDING_APPROVAL", reason="out of hours"),
        )

        core.handle(TEXT, SECTOR)

        (_request, rules, conflict) = gate.calls[0]
        assert len(rules.violations) == 1
        assert "working hours" in rules.violations[0]
        assert rules.missing_fields == ()
        # A request the pack rejected is never costed or conflict-checked.
        assert store.conflict_calls == []
        assert conflict is False


class TestGateDecisionIsAuthoritative:
    """The core never overrides the gate, even when the pack objected."""

    def test_confirming_a_violating_request_still_persists_a_real_slot(self):
        core, _, _, store, _ = build_core(fields=OUT_OF_HOURS_FIELDS)

        decision = core.handle(TEXT, SECTOR)

        assert decision.status == "CONFIRMED"
        # No slot was costed during rule evaluation (the pack objected), so
        # the core computes one here rather than persisting without a
        # footprint the store can later compare against.
        assert store.persisted[0][1] == SlotInfo(
            duration_minutes=30,
            capacity=1,
            resource_key="practitioner:Dr. A",
            start=OUT_OF_HOURS_FIELDS["start_time"],
        )

    def test_audit_still_reports_the_conflict_check_as_skipped(self):
        """Costing a slot late must not look like a conflict check ran."""
        core, _, _, store, audit = build_core(fields=OUT_OF_HOURS_FIELDS)

        core.handle(TEXT, SECTOR)

        assert store.conflict_calls == []
        assert (
            audit.records[0].rules_evaluated[2]
            == "conflict_check: skipped (rules not satisfied)"
        )


class TestAuditSurvivesFailuresAfterTheDecision:
    """One record per decided request, even when storing it then fails."""

    def test_a_failing_persist_still_leaves_exactly_one_record(self):
        core, _, _, store, audit = build_core()

        def exploding_persist(request, slot):
            raise RuntimeError("disk on fire")

        store.persist = exploding_persist

        with pytest.raises(RuntimeError):
            core.handle(TEXT, SECTOR)

        assert len(audit.records) == 1
        assert audit.records[0].decision == "CONFIRMED"

    def test_a_failing_late_slot_costing_still_leaves_exactly_one_record(self):
        """The pack refuses to cost a request it rejected; audit anyway."""
        core = SchedulingAgentCore(
            manifest_dir=SECTOR_FIXTURES,
            # Unknown practitioner: validate() reports a violation, and
            # slot_rules() then raises ValueError when the gate confirms
            # anyway, inside the persist step.
            skill_packs={PACK_ID: ClinicSkillPack()},
            intake=FakeIntake(dict(VALID_FIELDS, practitioner="Dr. Nobody")),
            gate=FakeGate(),
            store=(store := FakeStore()),
            audit=(audit := FakeAudit()),
        )

        with pytest.raises(ValueError):
            core.handle(TEXT, SECTOR)

        assert store.persisted == []
        assert len(audit.records) == 1
        assert audit.records[0].rules_evaluated[2] == (
            "conflict_check: skipped (rules not satisfied)"
        )


class TestMissingRequiredFields:
    """The generic missing-field check runs in the core, before the pack."""

    def test_missing_fields_reach_the_gate_and_the_pack_is_not_called(self):
        core, _, gate, store, _ = build_core(
            fields={"patient_name": "Sam Patel"},
            decision=Decision(status="PENDING_APPROVAL", reason="incomplete"),
        )

        # The real pack raises KeyError if validate() is called with these
        # fields absent -- reaching the gate at all proves the core
        # skipped validate() rather than crashing.
        core.handle(TEXT, SECTOR)

        (_request, rules, _conflict) = gate.calls[0]
        assert rules.missing_fields == ("practitioner", "start_time")
        assert rules.violations == ()
        assert store.conflict_calls == []

    def test_audit_record_shows_the_skipped_checks(self):
        core, _, _, _, audit = build_core(
            fields={"patient_name": "Sam Patel"},
            decision=Decision(status="PENDING_APPROVAL", reason="incomplete"),
        )

        core.handle(TEXT, SECTOR)

        rules_evaluated = audit.records[0].rules_evaluated
        assert rules_evaluated == [
            "required_fields: missing practitioner, start_time",
            "skill_pack_validation: skipped (required fields missing)",
            "conflict_check: skipped (rules not satisfied)",
        ]


class TestAuditRecordContent:
    """Every AuditRecord field is populated from the real flow."""

    def test_fields_are_built_from_request_manifest_and_decision(self):
        core, _, _, _, audit = build_core()

        core.handle(TEXT, SECTOR)
        record = audit.records[0]

        assert record.input == TEXT
        assert record.skill_pack == PACK_ID
        assert record.intent == VALID_FIELDS
        assert record.decision == "CONFIRMED"
        assert record.approval_status == "not_required"
        assert isinstance(record.timestamp, datetime)
        assert record.rules_evaluated == [
            "required_fields: ok",
            "skill_pack_validation: ok",
            "conflict_check: none",
        ]

    def test_intent_is_a_copy_not_the_live_request_fields(self):
        core, _, gate, _, audit = build_core()

        core.handle(TEXT, SECTOR)
        # The request the core actually built and passed on -- not the
        # module-level dict, which FakeIntake already copied.
        handled_request = gate.calls[0][0]
        audit.records[0].intent["practitioner"] = "tampered"

        assert handled_request.fields["practitioner"] == "Dr. A"


class TestConfigurationFailures:
    """Misconfiguration raises loudly; it never yields a silent no-op."""

    def test_unknown_sector_raises(self):
        core, _, _, _, _ = build_core()

        with pytest.raises(UnknownSectorError):
            core.handle(TEXT, "no_such_sector")

    def test_disabled_sector_raises(self):
        core, _, _, _, _ = build_core()

        with pytest.raises(SectorDisabledError):
            core.handle(TEXT, "paused")

    def test_unresolvable_skill_pack_raises(self):
        core, _, _, _, _ = build_core()

        with pytest.raises(UnknownSkillPackError):
            core.handle(TEXT, "orphan")

    def test_malformed_manifest_raises_an_orchestration_error(self):
        """A manifest typo must not escape as a raw ManifestValidationError."""
        core, _, _, _, _ = build_core()

        with pytest.raises(InvalidManifestError) as caught:
            core.handle(TEXT, "malformed")

        # The loader's own diagnosis is preserved, not swallowed.
        assert isinstance(caught.value.__cause__, ManifestValidationError)
        assert "skill_pack" in str(caught.value)

    @pytest.mark.parametrize(
        "sector",
        ["../paused", "..", "sub/clinic", "sub\\clinic", ""],
    )
    def test_sector_names_cannot_escape_the_manifest_directory(self, sector):
        core, _, _, _, _ = build_core()

        with pytest.raises(UnknownSectorError):
            core.handle(TEXT, sector)

    def test_a_traversing_sector_name_is_rejected_even_when_it_would_resolve(
        self, tmp_path
    ):
        """The guard rejects by name, before any file is looked at."""
        nested = tmp_path / "nested"
        nested.mkdir()
        (tmp_path / f"{SECTOR}.yaml").write_bytes(
            (SECTOR_FIXTURES / f"{SECTOR}.yaml").read_bytes()
        )
        core, _, _, _, _ = build_core(sector_dir=nested)

        with pytest.raises(UnknownSectorError):
            core.handle(TEXT, f"../{SECTOR}")

    def test_all_configuration_errors_share_a_base_class(self):
        for error in (
            UnknownSectorError,
            InvalidManifestError,
            SectorDisabledError,
            UnknownSkillPackError,
        ):
            assert issubclass(error, OrchestrationError)

    def test_configuration_failure_persists_and_audits_nothing(self):
        core, _, _, store, audit = build_core()

        with pytest.raises(OrchestrationError):
            core.handle(TEXT, "no_such_sector")

        assert store.persisted == []
        assert audit.records == []


class TestSkillPackResolution:
    """The manifest's identifier string, and only it, selects the pack."""

    def test_a_lazy_mapping_is_asked_once_for_the_manifests_identifier(self):
        """A Mapping that builds packs on demand must not build twice.

        Uses a real `collections.abc.Mapping`, whose `__contains__` is
        implemented via `__getitem__` -- so a contains-check-then-subscript
        resolver would show up here as two constructions.
        """
        requested = []

        class LazyPacks(Mapping):
            def __getitem__(self, key):
                requested.append(key)
                if key != PACK_ID:
                    raise KeyError(key)
                return ClinicSkillPack()

            def __iter__(self):
                return iter([PACK_ID])

            def __len__(self):
                return 1

        core = SchedulingAgentCore(
            manifest_dir=SECTOR_FIXTURES,
            skill_packs=LazyPacks(),
            intake=FakeIntake(VALID_FIELDS),
            gate=FakeGate(),
            store=FakeStore(),
            audit=FakeAudit(),
        )

        core.handle(TEXT, SECTOR)

        assert requested == [PACK_ID]

    def test_a_lazy_mapping_that_has_no_entry_raises_unknown_skill_pack(self):
        class EmptyPacks(Mapping):
            def __getitem__(self, key):
                raise KeyError(key)

            def __iter__(self):
                return iter([])

            def __len__(self):
                return 0

        core = SchedulingAgentCore(
            manifest_dir=SECTOR_FIXTURES,
            skill_packs=EmptyPacks(),
            intake=FakeIntake(VALID_FIELDS),
            gate=FakeGate(),
            store=FakeStore(),
            audit=FakeAudit(),
        )

        with pytest.raises(UnknownSkillPackError):
            core.handle(TEXT, SECTOR)

    def test_a_pack_the_core_has_never_heard_of_still_works(self):
        """Any SkillPack under any identifier -- no registry, no import."""

        class UnrelatedPack(SkillPack):
            required_fields = ["thing"]
            working_hours = {}

            def validate(self, request):
                return []

            def slot_rules(self, request):
                return SlotInfo(
                    duration_minutes=15,
                    capacity=2,
                    resource_key="thing:widget",
                    start=datetime(2026, 8, 5, 10, 30),
                )

            def confirmation_template(self):
                return "ok"

        core = SchedulingAgentCore(
            manifest_dir=SECTOR_FIXTURES,
            skill_packs={PACK_ID: UnrelatedPack()},
            intake=FakeIntake({"thing": "widget"}),
            gate=FakeGate(),
            store=FakeStore(),
            audit=FakeAudit(),
        )

        decision = core.handle(TEXT, SECTOR)

        assert decision.status == "CONFIRMED"


class TestInterfacesAreAbstract:
    """abc actually enforces each contract, so later tasks cannot half-implement."""

    @pytest.mark.parametrize(
        "interface", [IntakeService, ApprovalGate, BookingStore, AuditTrail]
    )
    def test_cannot_instantiate_directly(self, interface):
        with pytest.raises(TypeError):
            interface()

    def test_incomplete_store_cannot_be_instantiated(self):
        class HalfStore(BookingStore):
            def check_conflict(self, request, slot):
                return False

            # persist intentionally not implemented

        with pytest.raises(TypeError):
            HalfStore()


class TestRuleContext:
    """The gate's rule bundle is immutable and defaults to empty."""

    def test_defaults_are_empty(self):
        rules = RuleContext()

        assert rules.missing_fields == ()
        assert rules.violations == ()
        assert rules.approval_required_for == ()

    def test_is_frozen(self):
        rules = RuleContext(violations=("nope",))

        with pytest.raises(FrozenInstanceError):
            rules.violations = ()


class TestCoreHasNoSectorNames:
    """FR1 down payment: no sector name may appear in core/ source.

    Deliberately simple -- T11 builds the comprehensive R1-proof test.
    """

    def test_no_sector_name_in_any_core_module(self):
        core_dir = Path(orchestrator_module.__file__).parent
        pattern = re.compile(r"clinic|restaurant", re.IGNORECASE)

        offenders = {}
        modules = sorted(core_dir.glob("*.py"))
        for module in modules:
            hits = [
                f"{module.name}:{number}: {line.strip()}"
                for number, line in enumerate(
                    module.read_text(encoding="utf-8").splitlines(), start=1
                )
                if pattern.search(line)
            ]
            if hits:
                offenders[module.name] = hits

        assert modules, f"no core modules found to scan in {core_dir}"
        assert offenders == {}, f"sector names found in core/: {offenders}"
