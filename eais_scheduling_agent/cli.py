"""Command-line interface for the scheduling agent (T14 -- the wiring layer).

Constructs a real `SchedulingAgentCore` (see `core.orchestrator`) with every
concrete collaborator plugged in -- the clinic and restaurant skill packs,
the standard approval gate, the in-memory booking store, the JSON Lines
audit trail, and (by default) the deterministic offline intake -- and
exposes it as the `eais-book` console command (see `[project.scripts]` in
`pyproject.toml`).

Sector names are assembled in `wiring.py`, shared with `http_api.py`; this
module consumes that wiring rather than defining it. Per T6's report, "the
core deliberately does not render text" -- `SchedulingAgentCore.handle()`
returns a bare `Decision`, never a rendered message. Turning a CONFIRMED
decision into a human-readable string via the matching skill pack's
`confirmation_template()` is an entry point's job, implemented here and in
`http_api.py` identically via `wiring.render_confirmation()`.

Usage:
    eais-book <sector> <text> [--llm] [--audit-file PATH] [--manifest-dir DIR]

Example:
    eais-book clinic "Dr. A today at 10am, patient John Doe"

("Dr. A" and "Dr. B" are the bundled `ClinicSkillPack` defaults --
see `skillpacks/clinic/pack.py`'s `_DEFAULT_PRACTITIONERS` -- so this
example reaches CONFIRMED out of the box. Any other practitioner name
is a valid, non-crashing PENDING_APPROVAL, not an error.)
"""

import argparse
import sys
from typing import Dict, Optional, Sequence

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.gate import StandardApprovalGate
from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.orchestrator import OrchestrationError, SchedulingAgentCore
from eais_scheduling_agent.core.store import InMemoryBookingStore
from eais_scheduling_agent.intake.llm import LLMIntake
from eais_scheduling_agent.intake.offline import OfflineIntake
from eais_scheduling_agent.skillpacks.base import SkillPack

#: Matches the .gitignore entry for the default audit file, so a default
#: run doesn't need an extra flag to avoid polluting the repo.
_DEFAULT_AUDIT_FILE = "audit.jsonl"

_CONFIRMED = "CONFIRMED"
_PENDING_APPROVAL = "PENDING_APPROVAL"


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
            'Free-text booking request, e.g. "Dr. A today at 10am, '
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
        default=str(wiring.DEFAULT_MANIFEST_DIR),
        metavar="DIR",
        help=(
            "Directory holding one '<sector>.yaml' manifest per sector "
            "(default: the package's bundled manifests directory)."
        ),
    )
    return parser


def _render_confirmation(
    args: argparse.Namespace,
    intake: IntakeService,
    skill_packs: Dict[str, SkillPack],
) -> str:
    """Build the human-readable CONFIRMED message for this request.

    Per this module's docstring: the core never renders text, so this is
    the CLI's own job. `intake.parse` here is a cache hit (see
    `wiring.CachingIntake`) against the exact call `SchedulingAgentCore.handle`
    already made, so this does not re-run the LLM or the regex parser.
    """
    manifest = wiring.load_manifest_for_render(args.manifest_dir, args.sector)
    skill_pack = skill_packs[manifest.skill_pack]
    request = intake.parse(args.text, args.sector)
    return wiring.render_confirmation(skill_pack, request)


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
    real_intake: IntakeService = (
        LLMIntake(fallback=offline, client=wiring.build_llm_client())
        if args.llm
        else offline
    )
    intake: IntakeService = wiring.CachingIntake(real_intake)
    skill_packs = wiring.build_skill_packs()

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
