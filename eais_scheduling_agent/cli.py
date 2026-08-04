"""Command-line interface for the scheduling agent (T14 -- the wiring layer).

Constructs a real `SchedulingAgentCore` (see `core.orchestrator`) with every
concrete collaborator plugged in -- the clinic and restaurant skill packs,
the standard approval gate, the in-memory booking store, the JSON Lines
audit trail, and (by default) the deterministic offline intake -- and
exposes it as the `eais-book` console command (see `[project.scripts]` in
`pyproject.toml`).

This is the one place in the project allowed to name sectors: every prior
task (T6-T13) built one interchangeable piece behind `core.interfaces` /
`skillpacks.base`, and none of them may import a concrete sector. This
module is where "clinic" and "restaurant" are finally named and assembled
into something runnable -- per T6's report: "T14 -- Wiring. This is the
layer that is allowed to name sectors."

Per T6's report, "the core deliberately does not render text" --
`SchedulingAgentCore.handle()` returns a bare `Decision`, never a rendered
message. Turning a CONFIRMED decision into a human-readable string via the
matching skill pack's `confirmation_template()` is this module's job.

Usage:
    eais-book <sector> <text> [--llm] [--audit-file PATH] [--manifest-dir DIR]

Example:
    eais-book clinic "Dr. Salem today at 10am, patient John Doe"
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.gate import StandardApprovalGate
from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.core.orchestrator import OrchestrationError, SchedulingAgentCore
from eais_scheduling_agent.core.store import InMemoryBookingStore
from eais_scheduling_agent.intake.llm import LLMIntake
from eais_scheduling_agent.intake.offline import OfflineIntake
from eais_scheduling_agent.manifests.manifest import ManifestValidationError, SectorManifest
from eais_scheduling_agent.skillpacks.base import SkillPack
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack

#: Bundled production manifests directory -- one <sector>.yaml per sector,
#: shipped as package data (see [tool.setuptools.package-data] in
#: pyproject.toml). Resolved relative to this file so it works the same
#: whether the package is installed editable (`pip install -e .`) or as a
#: real wheel/sdist install.
_DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"

#: Matches the .gitignore entry for the default audit file, so a default
#: run doesn't need an extra flag to avoid polluting the repo.
_DEFAULT_AUDIT_FILE = "audit.jsonl"

#: Manifest file suffixes to try, in the same priority order
#: `SchedulingAgentCore._load_manifest` uses internally. Duplicated here
#: only for the render-time manifest re-read in `_load_manifest_for_render`
#: below -- never for the orchestration decision itself, which the core
#: alone makes.
_MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")

_CONFIRMED = "CONFIRMED"
_PENDING_APPROVAL = "PENDING_APPROVAL"


def _skill_packs() -> Dict[str, SkillPack]:
    """Build the `skill_pack` identifier -> instance mapping this CLI knows.

    The only place in the project that maps a manifest's opaque
    `skill_pack` string to a concrete class -- see this module's docstring.
    """
    return {
        "clinic_v1": ClinicSkillPack(),
        "restaurant_v1": RestaurantSkillPack(),
    }


class _CachingIntake(IntakeService):
    """Wraps a real `IntakeService`, memoizing by the exact `(text, sector)` pair.

    `SchedulingAgentCore.handle()` returns only a `Decision` -- never the
    `BookingRequest` it built internally (see
    `core.orchestrator.SchedulingAgentCore.handle`'s return type). But
    rendering a CONFIRMED booking's `confirmation_template()` needs exactly
    that request's `fields`. Re-deriving them by calling `intake.parse()` a
    second, independent time would cost a second LLM round-trip under
    `--llm`, and -- if the model's sampling is not perfectly stable --
    could theoretically return *different* fields than the ones the core
    actually validated and persisted, which would make the printed
    confirmation lie about what was actually booked.

    Instead, this wrapper caches the first call for a given `(text,
    sector)` pair and returns the same `BookingRequest` object on every
    later call with the same arguments. `main()` calls `parse()` once
    itself, after `handle()` returns CONFIRMED, passing the identical
    `text`/`sector` that `handle()` already used internally -- so it gets
    the cached result back without parsing (or calling an LLM) again.
    """

    def __init__(self, inner: IntakeService) -> None:
        self._inner = inner
        self._cache: Dict[Tuple[str, str], BookingRequest] = {}

    def parse(self, text: str, sector: str) -> BookingRequest:
        key = (text, sector)
        if key not in self._cache:
            self._cache[key] = self._inner.parse(text, sector)
        return self._cache[key]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eais-book",
        description=(
            "Run one scheduling request end to end from free text: parse "
            "it, validate it against its sector's rules, decide whether it "
            "auto-confirms or needs human approval, and record the outcome."
        ),
    )
    parser.add_argument(
        "sector",
        help=(
            "Which sector to book under, e.g. 'clinic' or 'restaurant'. "
            "Must match a '<sector>.yaml' manifest file name in "
            "--manifest-dir."
        ),
    )
    parser.add_argument(
        "text",
        help=(
            'Free-text booking request, e.g. "Dr. Salem today at 10am, '
            'patient John Doe".'
        ),
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Use LLM-backed intake (via a local Ollama server) instead of "
            "the default deterministic offline parser. Falls back to "
            "offline intake automatically if the LLM is unreachable or "
            "unreliable. Opt-in because no local model is guaranteed to be "
            "running."
        ),
    )
    parser.add_argument(
        "--audit-file",
        default=_DEFAULT_AUDIT_FILE,
        metavar="PATH",
        help=(
            "Path to the JSON Lines audit file to append to "
            f"(default: {_DEFAULT_AUDIT_FILE})."
        ),
    )
    parser.add_argument(
        "--manifest-dir",
        default=str(_DEFAULT_MANIFEST_DIR),
        metavar="DIR",
        help=(
            "Directory holding one '<sector>.yaml' manifest per sector "
            "(default: the package's bundled manifests directory)."
        ),
    )
    return parser


def _load_manifest_for_render(manifest_dir: str, sector: str) -> SectorManifest:
    """Re-read the sector's manifest, purely to learn its `skill_pack` id.

    Only called after `core.handle()` has already succeeded for the same
    `sector`, so the manifest is known to exist and be valid at this point
    -- this never has to handle the error cases `SchedulingAgentCore`
    itself already handled internally (see `OrchestrationError` and its
    subclasses in `core.orchestrator`). A second, cheap, read-only file
    read -- not a duplicate of any decision-making, which stays entirely
    the core's.
    """
    base = Path(manifest_dir)
    for suffix in _MANIFEST_SUFFIXES:
        candidate = base / f"{sector}{suffix}"
        if candidate.is_file():
            return SectorManifest.load(str(candidate))
    raise ManifestValidationError(
        f"manifest for sector {sector!r} unexpectedly missing from "
        f"{manifest_dir} after a successful booking decision"
    )


def _render_confirmation(
    args: argparse.Namespace,
    intake: IntakeService,
    skill_packs: Dict[str, SkillPack],
) -> str:
    """Build the human-readable CONFIRMED message for this request.

    Per this module's docstring: the core never renders text, so this is
    the CLI's own job. `intake.parse` here is a cache hit (see
    `_CachingIntake`) against the exact call `SchedulingAgentCore.handle`
    already made, so this does not re-run the LLM or the regex parser.
    """
    manifest = _load_manifest_for_render(args.manifest_dir, args.sector)
    skill_pack = skill_packs[manifest.skill_pack]
    request = intake.parse(args.text, args.sector)
    return skill_pack.confirmation_template().format(**request.fields)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns a process exit code (0 success, nonzero error).

    Registered as the `eais-book` console script (see `[project.scripts]`
    in `pyproject.toml`); setuptools' generated wrapper calls
    `sys.exit(main())`.

    `argv`: explicit argument list (used by tests, in place of patching
    `sys.argv`); defaults to `sys.argv[1:]` via `argparse` when omitted.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    offline = OfflineIntake()
    real_intake: IntakeService = LLMIntake(fallback=offline) if args.llm else offline
    intake: IntakeService = _CachingIntake(real_intake)
    skill_packs = _skill_packs()

    core = SchedulingAgentCore(
        manifest_dir=args.manifest_dir,
        skill_packs=skill_packs,
        intake=intake,
        gate=StandardApprovalGate(),
        store=InMemoryBookingStore(),
        audit=JsonLinesAuditTrail(path=args.audit_file),
    )

    try:
        decision = core.handle(args.text, args.sector)
    except OrchestrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if decision.status == _CONFIRMED:
        print(_render_confirmation(args, intake, skill_packs))
    else:
        print(f"Pending approval: {decision.reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
