"""Shared wiring: the one place `cli.py` and `http_api.py` both draw from
to name sectors and assemble skill packs, so neither entry point defines
this mapping independently. See `docs/superpowers/specs/2026-08-05-http-interface-design.md`.

Before this module existed, `cli.py` was documented as "the one place in
the project allowed to name sectors" (T14). That claim now belongs here:
`cli.py` and `http_api.py` both *consume* this module, neither *defines*
the sector-naming knowledge on its own.
"""

from pathlib import Path
from typing import Dict, Tuple, Union

from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.manifests.manifest import (
    ManifestValidationError,
    SectorManifest,
)
from eais_scheduling_agent.skillpacks.base import SkillPack
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack

#: Bundled production manifests directory -- one <sector>.yaml per sector,
#: shipped as package data (see [tool.setuptools.package-data] in
#: pyproject.toml). Resolved relative to this file so it works the same
#: whether the package is installed editable or as a real wheel/sdist.
DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"

#: Manifest file suffixes to try, in the same priority order
#: `SchedulingAgentCore._load_manifest` uses internally. Duplicated here
#: only for this module's render-time manifest re-read, never for the
#: orchestration decision itself, which the core alone makes.
_MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")


def build_skill_packs() -> Dict[str, SkillPack]:
    """Build the `skill_pack` identifier -> instance mapping both entry points share.

    The only place in the project that maps a manifest's opaque
    `skill_pack` string to a concrete class -- see this module's docstring.
    """
    return {
        "clinic_v1": ClinicSkillPack(),
        "restaurant_v1": RestaurantSkillPack(),
    }


def load_manifest_for_render(manifest_dir: Union[str, Path], sector: str) -> SectorManifest:
    """Re-read a sector's manifest, purely to learn its `skill_pack` id.

    Intended to be called only after `core.handle()` has already
    succeeded for the same `sector`, so the manifest is known to exist
    and be valid at this point -- a second, cheap, read-only file read,
    never a duplicate of any decision-making (which stays entirely the
    core's).
    """
    base = Path(manifest_dir)
    for suffix in _MANIFEST_SUFFIXES:
        candidate = base / f"{sector}{suffix}"
        if candidate.is_file():
            return SectorManifest.load(str(candidate))
    raise ManifestValidationError(
        f"manifest for sector {sector!r} unexpectedly missing from {manifest_dir}"
    )


def render_confirmation(skill_pack: SkillPack, request: BookingRequest) -> str:
    """Build the human-readable CONFIRMED message for one request.

    The core deliberately does not render text (see `core/orchestrator.py`)
    -- turning a CONFIRMED decision into a message via the matching skill
    pack's `confirmation_template()` is an entry point's job, shared here
    so `cli.py` and `http_api.py` do it identically.
    """
    return skill_pack.confirmation_template().format(**request.fields)


class CachingIntake(IntakeService):
    """Wraps a real `IntakeService`, memoizing by the exact `(text, sector)` pair.

    `SchedulingAgentCore.handle()` returns only a `Decision` -- never the
    `BookingRequest` it built internally. But rendering a CONFIRMED
    booking's `confirmation_template()` needs exactly that request's
    `fields`. Re-deriving them by calling `intake.parse()` a second,
    independent time would cost a second LLM round-trip when LLM intake
    is in use, and -- if the model's sampling is not perfectly stable --
    could theoretically return *different* fields than the ones the core
    actually validated and persisted, which would make the rendered
    confirmation lie about what was actually booked.

    Instead, this wrapper caches the first call for a given `(text,
    sector)` pair and returns the same `BookingRequest` object on every
    later call with the same arguments.
    """

    def __init__(self, inner: IntakeService) -> None:
        self._inner = inner
        self._cache: Dict[Tuple[str, str], BookingRequest] = {}

    def parse(self, text: str, sector: str) -> BookingRequest:
        key = (text, sector)
        if key not in self._cache:
            self._cache[key] = self._inner.parse(text, sector)
        return self._cache[key]
