# HTTP Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional HTTP interface (`POST /bookings`, `GET /audit`) alongside the existing `eais-book` CLI, without adding any new decision logic to `core/` and without changing the CLI's default (Flask-free) install path.

**Architecture:** Extract the sector-naming wiring `cli.py` currently owns privately (`_skill_packs()`, `_CachingIntake`, `_load_manifest_for_render`) into a new shared module, `eais_scheduling_agent/wiring.py`. A new `eais_scheduling_agent/http_api.py` builds a Flask app around two `SchedulingAgentCore` instances (offline-intake and LLM-intake) that share one `InMemoryBookingStore`/`JsonLinesAuditTrail`/skill-pack mapping/gate — so cross-request conflict detection works for the server's lifetime, unlike the CLI's per-process-fresh-store behavior.

**Tech Stack:** Python (existing), Flask >= 3.0 as a new **optional** dependency (`pip install -e ".[http]"`), pytest + Flask's `app.test_client()` for tests (no real sockets).

## Global Constraints

- Every task lands on a feature branch off `develop` (already checked out: `feature/http-interface`), gets its own commit(s), and must pass the full existing test suite (`pytest`) plus any new tests before being considered done.
- `core/` is never modified by this plan. If any task seems to require touching `core/`, stop — that would mean the design is wrong, not that this constraint should bend.
- Flask is an **optional** dependency (`[project.optional-dependencies] http = [...]`), never added to `dependencies = [...]`. The base `pip install -e .` path must keep working exactly as it does today, with no import of `flask` anywhere reachable from `cli.py` or `core/`.
- No new decision logic anywhere in this plan — `http_api.py` only ever calls `SchedulingAgentCore.handle()` and renders/serializes its result, mirroring what `cli.py` already does.
- All new tests avoid real network calls, matching the project's existing "no network access in tests" discipline (verified today by grepping for `urllib`/`socket`/`requests`/`http.client` in `tests/`).
- Design spec: `docs/superpowers/specs/2026-08-05-http-interface-design.md` — every task below implements one section of it; consult it for the *why* behind a decision if a step's rationale isn't obvious from the code alone.

---

### Task 1: Extract shared wiring into `wiring.py`

**Files:**
- Create: `eais_scheduling_agent/wiring.py`
- Create: `tests/test_wiring.py`
- Modify: `eais_scheduling_agent/cli.py`

**Interfaces:**
- Produces (consumed by Task 2 and by `cli.py`'s own refactor in this task):
  - `wiring.DEFAULT_MANIFEST_DIR: Path` — the package's bundled manifests directory.
  - `wiring.build_skill_packs() -> Dict[str, SkillPack]` — `{"clinic_v1": ClinicSkillPack(), "restaurant_v1": RestaurantSkillPack()}`.
  - `wiring.load_manifest_for_render(manifest_dir: Union[str, Path], sector: str) -> SectorManifest` — raises `eais_scheduling_agent.manifests.manifest.ManifestValidationError` if no manifest file is found.
  - `wiring.render_confirmation(skill_pack: SkillPack, request: BookingRequest) -> str` — `skill_pack.confirmation_template().format(**request.fields)`.
  - `wiring.CachingIntake(inner: IntakeService)` — an `IntakeService` that memoizes `parse()` by the exact `(text, sector)` pair, so a second call with identical arguments never re-invokes `inner`.

- [ ] **Step 1: Write failing tests for `build_skill_packs` and `render_confirmation`**

Create `tests/test_wiring.py`:

```python
"""Tests for the shared wiring module (sector-naming, shared between
cli.py and http_api.py -- see docs/superpowers/specs/2026-08-05-http-interface-design.md).
"""

from datetime import datetime

import pytest

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.manifests.manifest import ManifestValidationError
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack


class TestBuildSkillPacks:
    def test_maps_clinic_and_restaurant_identifiers(self):
        packs = wiring.build_skill_packs()
        assert isinstance(packs["clinic_v1"], ClinicSkillPack)
        assert isinstance(packs["restaurant_v1"], RestaurantSkillPack)


class TestRenderConfirmation:
    def test_formats_template_with_request_fields(self):
        skill_pack = ClinicSkillPack()
        request = BookingRequest(
            sector="clinic",
            fields={
                "patient_name": "John Doe",
                "practitioner": "Dr. A",
                "start_time": datetime(2026, 8, 5, 10, 0, 0),
            },
            raw_text="Dr. A today at 10am, patient John Doe",
        )

        message = wiring.render_confirmation(skill_pack, request)

        assert message == "Confirmed: John Doe with Dr. A at 2026-08-05 10:00:00."
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_wiring.py -v`
Expected: `FAIL` / `ERROR` — `ModuleNotFoundError: No module named 'eais_scheduling_agent.wiring'`

- [ ] **Step 3: Create `wiring.py` with `DEFAULT_MANIFEST_DIR`, `build_skill_packs`, `render_confirmation`**

Create `eais_scheduling_agent/wiring.py`:

```python
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
```

- [ ] **Step 4: Run to verify Step 1's tests pass**

Run: `python -m pytest tests/test_wiring.py -v`
Expected: `PASS` (2 tests)

- [ ] **Step 5: Write failing tests for `load_manifest_for_render` and `CachingIntake`**

Append to `tests/test_wiring.py`:

```python
class TestLoadManifestForRender:
    def test_loads_real_clinic_manifest(self):
        manifest = wiring.load_manifest_for_render(str(wiring.DEFAULT_MANIFEST_DIR), "clinic")
        assert manifest.skill_pack == "clinic_v1"

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(ManifestValidationError):
            wiring.load_manifest_for_render(str(tmp_path), "veterinary")


class _CountingFakeIntake(IntakeService):
    def __init__(self):
        self.calls = 0

    def parse(self, text, sector):
        self.calls += 1
        return BookingRequest(sector=sector, fields={"call": self.calls}, raw_text=text)


class TestCachingIntake:
    def test_second_call_with_same_args_is_a_cache_hit(self):
        inner = _CountingFakeIntake()
        caching = wiring.CachingIntake(inner)

        first = caching.parse("some text", "clinic")
        second = caching.parse("some text", "clinic")

        assert first is second
        assert inner.calls == 1

    def test_different_args_are_not_cached_together(self):
        inner = _CountingFakeIntake()
        caching = wiring.CachingIntake(inner)

        caching.parse("text a", "clinic")
        caching.parse("text b", "clinic")

        assert inner.calls == 2
```

(No implementation changes needed for this step — `load_manifest_for_render` and `CachingIntake` were already written in Step 3. Confirm below that these tests pass against that existing code.)

- [ ] **Step 6: Run to verify all of `test_wiring.py` passes**

Run: `python -m pytest tests/test_wiring.py -v`
Expected: `PASS` (6 tests)

- [ ] **Step 7: Refactor `cli.py` to consume `wiring.py` instead of defining its own copies**

In `eais_scheduling_agent/cli.py`:

Remove these imports (no longer used directly in this file):
```python
from eais_scheduling_agent.manifests.manifest import ManifestValidationError, SectorManifest
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack
```

Add:
```python
from eais_scheduling_agent import wiring
```

Change the `typing` import line from:
```python
from typing import Dict, Optional, Sequence, Tuple
```
to:
```python
from typing import Dict, Optional, Sequence
```
(`Tuple` was only used by `_CachingIntake`'s cache-key type hint, which moves to `wiring.py` in this step.)

Remove these module-level definitions entirely: `_DEFAULT_MANIFEST_DIR`, `_MANIFEST_SUFFIXES`, `_skill_packs()`, `_CachingIntake` (the whole class), `_load_manifest_for_render()`.

In `_build_parser()`, change:
```python
        default=str(_DEFAULT_MANIFEST_DIR),
```
to:
```python
        default=str(wiring.DEFAULT_MANIFEST_DIR),
```

Replace `_render_confirmation()`'s body:
```python
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
```

In `main()`, change:
```python
    intake: IntakeService = _CachingIntake(real_intake)
    skill_packs = _skill_packs()
```
to:
```python
    intake: IntakeService = wiring.CachingIntake(real_intake)
    skill_packs = wiring.build_skill_packs()
```

Update the module docstring's claim (near the top of the file) from "This is the one place in the project allowed to name sectors" to: "Sector names are assembled in `wiring.py`, shared with `http_api.py`; this module consumes that wiring rather than defining it."

- [ ] **Step 8: Run the full existing CLI test suite to verify no regression**

Run: `python -m pytest tests/test_cli.py -v`
Expected: `PASS` (5 tests, unchanged from before this refactor — same assertions, same behavior, just re-wired internals)

- [ ] **Step 9: Run the full test suite**

Run: `python -m pytest`
Expected: `PASS`, same total count as before plus the 6 new `test_wiring.py` tests

- [ ] **Step 10: Commit**

```bash
git add eais_scheduling_agent/wiring.py eais_scheduling_agent/cli.py tests/test_wiring.py
git commit -m "Extract shared sector-naming wiring into wiring.py

cli.py's private _skill_packs()/_CachingIntake/_load_manifest_for_render
move into a new shared module so a second entry point (the upcoming
HTTP interface) can reuse them without duplicating the sector-naming
mapping. Pure refactor -- no behavior change; tests/test_cli.py passes
unmodified."
```

---

### Task 2: HTTP API module (`http_api.py`)

**Files:**
- Modify: `pyproject.toml` (add `[project.optional-dependencies] http`)
- Create: `eais_scheduling_agent/http_api.py`
- Create: `tests/test_http_api.py`

**Interfaces:**
- Consumes: `wiring.DEFAULT_MANIFEST_DIR`, `wiring.build_skill_packs()`, `wiring.load_manifest_for_render()`, `wiring.render_confirmation()`, `wiring.CachingIntake` (all from Task 1); `SchedulingAgentCore`, `UnknownSectorError`, `SectorDisabledError`, `InvalidManifestError`, `UnknownSkillPackError` (from `core.orchestrator`); `StandardApprovalGate` (`core.gate`); `InMemoryBookingStore` (`core.store`); `JsonLinesAuditTrail` (`core.audit`); `OfflineIntake` (`intake.offline`); `LLMIntake` (`intake.llm`).
- Produces: `http_api.create_app(manifest_dir=wiring.DEFAULT_MANIFEST_DIR, audit_file="audit.jsonl") -> flask.Flask`, consumed by Task 3's console script and by `tests/test_http_api.py`.

- [ ] **Step 1: Add Flask as an optional dependency**

In `pyproject.toml`, add a new table right after `[project.optional-dependencies]`'s existing `dev` entry:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-cov>=4.0",
]
http = [
    "flask>=3.0",
]
```

Install it into the dev environment so the tests in this task can run:
```bash
pip install -e ".[http]"
```

- [ ] **Step 2: Write failing tests for `POST /bookings`**

Create `tests/test_http_api.py`:

```python
"""Tests for the optional HTTP interface (eais_scheduling_agent.http_api).

Uses Flask's built-in `test_client()` exclusively -- no real socket is
opened at any point, matching the project's existing "no network access
in tests" discipline (see README.md's "Run tests" section).
"""

import pytest

from eais_scheduling_agent.http_api import create_app


@pytest.fixture
def client(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    app = create_app(audit_file=str(audit_path))
    app.testing = True
    return app.test_client()


class TestClinicBookingConfirmed:
    def test_confirms_and_returns_message(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "CONFIRMED"
        assert "John Doe" in body["message"]
        assert "Dr. A" in body["message"]


class TestRestaurantBookingConfirmed:
    def test_confirms_and_returns_message(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm, customer Jane Smith"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "CONFIRMED"
        assert "Jane Smith" in body["message"]


class TestPendingApproval:
    def test_outside_working_hours_returns_reason(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 6am, patient John Doe"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "PENDING_APPROVAL"
        assert "outside working hours" in body["reason"]


class TestUnknownSector:
    def test_returns_404(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "veterinary", "text": "some request text"},
        )
        assert response.status_code == 404
        assert "veterinary" in response.get_json()["error"]


class TestMalformedBody:
    def test_missing_text_returns_400(self, client):
        response = client.post("/bookings", json={"sector": "clinic"})
        assert response.status_code == 400

    def test_non_json_body_returns_400(self, client):
        response = client.post("/bookings", data="not json", content_type="text/plain")
        assert response.status_code == 400


class TestLLMFlagFallsBackCleanly:
    """No local Ollama server is installed in this environment or CI
    (same situation `tests/test_cli.py::TestLLMFlagFallsBackCleanly`
    documents) -- `"llm": true` still confirms via automatic fallback.
    """

    def test_llm_true_falls_back_and_still_confirms(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe", "llm": True},
        )
        assert response.status_code == 200
        assert response.get_json()["status"] == "CONFIRMED"
```

- [ ] **Step 3: Run to verify it fails**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: `FAIL` / `ERROR` — `ModuleNotFoundError: No module named 'eais_scheduling_agent.http_api'`

- [ ] **Step 4: Create `http_api.py` with `create_app()` and `POST /bookings`**

Create `eais_scheduling_agent/http_api.py`:

```python
"""Optional HTTP interface for the scheduling agent.

A thin Flask wrapper around the same `SchedulingAgentCore` the CLI (T14)
uses -- no new decision logic lives here; `core/` is untouched by this
module. See docs/superpowers/specs/2026-08-05-http-interface-design.md
for the full design rationale.

The one deliberate behavioral difference from the CLI: `create_app()`
builds its collaborators once, at app-creation time, and holds them for
the server's lifetime. The CLI builds a fresh `InMemoryBookingStore` per
process (per invocation), so two separate `eais-book` calls can never
conflict with each other; a long-running server is one continuous
process, so sharing one store here makes cross-request conflict
detection real for as long as the server runs (see
`tests/test_http_api.py::TestSharedStoreAcrossRequests`).

Two `SchedulingAgentCore` instances are built -- one offline-intake, one
LLM-intake -- because `SchedulingAgentCore.handle()` has no per-call way
to select intake mode; `POST /bookings`'s per-request `"llm"` flag picks
which core handles that request. Both cores are wired to the *same*
`skill_packs`, `gate`, `store`, and `audit` instances, so which core
handles a given request never affects shared-state consistency. Both
cores' intake is wrapped in `wiring.CachingIntake` at app-creation time
(not per-request) so the second `intake.parse()` call this module makes
to render a CONFIRMED message is always a cache hit against the exact
call `core.handle()` already made -- this cache lives for the server's
lifetime, which is an accepted, documented trade-off for a prototype
(unbounded by request volume, not by wall-clock time or request count),
not a correctness issue: the project's own `RestaurantSkillPack` static
table-assignment trade-off in `DESIGN.md` Section 3 is the same kind of
disclosed simplification.
"""

import json
from pathlib import Path
from typing import Union

from flask import Flask, jsonify, request

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.gate import StandardApprovalGate
from eais_scheduling_agent.core.orchestrator import (
    InvalidManifestError,
    SchedulingAgentCore,
    SectorDisabledError,
    UnknownSectorError,
    UnknownSkillPackError,
)
from eais_scheduling_agent.core.store import InMemoryBookingStore
from eais_scheduling_agent.intake.llm import LLMIntake
from eais_scheduling_agent.intake.offline import OfflineIntake

_CONFIRMED = "CONFIRMED"


def create_app(
    manifest_dir: Union[str, Path] = wiring.DEFAULT_MANIFEST_DIR,
    audit_file: Union[str, Path] = "audit.jsonl",
) -> Flask:
    """Build a Flask app with one shared core/store for its whole lifetime.

    Args:
        manifest_dir: Directory holding one `<sector>.yaml` manifest per
            sector (default: the package's bundled manifests directory,
            same default `eais-book` uses).
        audit_file: JSON Lines audit file both `POST /bookings` appends
            to and `GET /audit` reads back (default: `audit.jsonl`,
            already git-ignored -- same default `eais-book` uses).
    """
    app = Flask(__name__)

    skill_packs = wiring.build_skill_packs()
    gate = StandardApprovalGate()
    store = InMemoryBookingStore()
    audit = JsonLinesAuditTrail(path=audit_file)

    offline_intake = wiring.CachingIntake(OfflineIntake())
    llm_intake = wiring.CachingIntake(LLMIntake(fallback=OfflineIntake()))

    offline_core = SchedulingAgentCore(
        manifest_dir=manifest_dir,
        skill_packs=skill_packs,
        intake=offline_intake,
        gate=gate,
        store=store,
        audit=audit,
    )
    llm_core = SchedulingAgentCore(
        manifest_dir=manifest_dir,
        skill_packs=skill_packs,
        intake=llm_intake,
        gate=gate,
        store=store,
        audit=audit,
    )

    @app.post("/bookings")
    def post_booking():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "sector" not in body or "text" not in body:
            return (
                jsonify({"error": "request body must be a JSON object with 'sector' and 'text'"}),
                400,
            )

        sector = body["sector"]
        text = body["text"]
        use_llm = bool(body.get("llm", False))
        core = llm_core if use_llm else offline_core
        intake = llm_intake if use_llm else offline_intake

        try:
            decision = core.handle(text, sector)
        except UnknownSectorError as exc:
            return jsonify({"error": str(exc)}), 404
        except SectorDisabledError as exc:
            return jsonify({"error": str(exc)}), 400
        except (InvalidManifestError, UnknownSkillPackError) as exc:
            return jsonify({"error": str(exc)}), 500

        if decision.status == _CONFIRMED:
            manifest = wiring.load_manifest_for_render(manifest_dir, sector)
            skill_pack = skill_packs[manifest.skill_pack]
            booking_request = intake.parse(text, sector)  # cache hit
            message = wiring.render_confirmation(skill_pack, booking_request)
            return jsonify({"status": "CONFIRMED", "message": message}), 200

        return jsonify({"status": "PENDING_APPROVAL", "reason": decision.reason}), 200

    return app
```

- [ ] **Step 5: Run to verify Step 2's tests pass**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: `PASS` (7 tests)

- [ ] **Step 6: Write failing tests for `GET /audit` and the shared-store conflict**

Append to `tests/test_http_api.py`:

```python
class TestAuditEndpoint:
    def test_returns_empty_list_before_any_booking(self, client):
        response = client.get("/audit")
        assert response.status_code == 200
        assert response.get_json()["records"] == []

    def test_returns_one_record_per_request(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm, customer Jane Smith"},
        )

        response = client.get("/audit")
        records = response.get_json()["records"]
        assert len(records) == 2
        assert records[0]["decision"] == "CONFIRMED"
        assert records[1]["decision"] == "CONFIRMED"


class TestSharedStoreAcrossRequests:
    """The one test that exercises the shared-store design decision
    directly: the CLI cannot show this (see tests/test_cli.py -- each
    invocation is a fresh process with a fresh store), but two requests
    through the *same* running server can genuinely conflict.
    """

    def test_second_booking_for_same_slot_conflicts(self, client):
        first = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        assert first.get_json()["status"] == "CONFIRMED"

        second = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient Second Patient"},
        )
        body = second.get_json()
        assert body["status"] == "PENDING_APPROVAL"
        assert "conflicts with an existing booking" in body["reason"]
```

- [ ] **Step 7: Run to verify these new tests fail**

Run: `python -m pytest tests/test_http_api.py -v -k "AuditEndpoint or SharedStore"`
Expected: `FAIL` — `GET /audit` returns `404` (route not yet defined); the shared-store test likely fails too since it depends on `/audit` only indirectly but should otherwise already pass once `/bookings` behaves correctly. Confirm both `TestAuditEndpoint` tests fail with a 404, and note whether `TestSharedStoreAcrossRequests` already passes (it may, since it only depends on `POST /bookings`, already implemented in Step 4).

- [ ] **Step 8: Add the `GET /audit` route**

In `eais_scheduling_agent/http_api.py`, add `import json` is already present at the top; add this route inside `create_app()`, after the `post_booking()` route (before `return app`):

```python
    @app.get("/audit")
    def get_audit():
        path = Path(audit_file)
        records = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return jsonify({"records": records}), 200
```

- [ ] **Step 9: Run to verify all of `test_http_api.py` passes**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: `PASS` (11 tests)

- [ ] **Step 10: Run the full test suite**

Run: `python -m pytest`
Expected: `PASS`, same as Task 1's end state plus the 11 new `test_http_api.py` tests. Also confirm the CLI-only path is untouched:

```bash
pip uninstall -y flask
python -m pytest tests/test_cli.py -v
```
Expected: still `PASS` — `cli.py` never imports `flask` or `http_api`, so removing Flask must not affect it. Reinstall afterward: `pip install -e ".[http]"`.

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml eais_scheduling_agent/http_api.py tests/test_http_api.py
git commit -m "Add optional HTTP interface: POST /bookings, GET /audit

Flask is an optional extra (pip install -e \".[http]\"), never a hard
dependency -- the CLI-only install path is unaffected. create_app()
holds one shared store for the server's lifetime, so two POST
/bookings calls to the same running server can genuinely conflict
with each other, unlike two separate eais-book CLI invocations."
```

---

### Task 3: `eais-book-server` console script + README

**Files:**
- Modify: `pyproject.toml` (add `[project.scripts]` entry)
- Modify: `README.md` (new "Run the HTTP API" section)

**Interfaces:**
- Consumes: `http_api.create_app` (Task 2).
- Produces: `eais-book-server` console command, runnable after `pip install -e ".[http]"`.

- [ ] **Step 1: Add the console script entry**

In `pyproject.toml`, change:
```toml
[project.scripts]
eais-book = "eais_scheduling_agent.cli:main"
```
to:
```toml
[project.scripts]
eais-book = "eais_scheduling_agent.cli:main"
eais-book-server = "eais_scheduling_agent.http_api:run"
```

Add a `run()` function to `eais_scheduling_agent/http_api.py`, right after `create_app()`:

```python
def run() -> None:
    """Console-script entry point (`eais-book-server`). Runs the dev server
    on 127.0.0.1:5000 with default manifest/audit locations -- adequate for
    the prototype/demo use this interface exists for; see the design spec's
    "Out of scope" section for what this deliberately does not add
    (HTTPS, auth, production WSGI server, etc.).
    """
    create_app().run()
```

- [ ] **Step 2: Reinstall and manually verify the console script works**

```bash
pip install -e ".[http]"
eais-book-server &
```

In a second terminal (or after backgrounding):
```bash
curl -X POST http://127.0.0.1:5000/bookings -H "Content-Type: application/json" -d "{\"sector\": \"clinic\", \"text\": \"Dr. A today at 10am, patient John Doe\"}"
```
Expected: `{"message":"Confirmed: John Doe with Dr. A at <today> 10:00:00.","status":"CONFIRMED"}`

```bash
curl http://127.0.0.1:5000/audit
```
Expected: `{"records":[{...one record...}]}`

Stop the server (`kill` the backgrounded process, or Ctrl+C if run in the foreground).

- [ ] **Step 3: Add a "Run the HTTP API" section to README.md**

In `README.md`, add a new section after the existing `### --llm mode` subsection and before `## Run tests`:

```markdown
## Run the HTTP API (optional)

The CLI above satisfies the brief's interface requirement on its own;
the HTTP interface is an additional, optional way to run the same
system, not a replacement.

Requires the `http` extra:

```
pip install -e ".[http]"
```

Start the server (default: `127.0.0.1:5000`):

```
eais-book-server
```

or, if the console script is not on `PATH` (same Windows caveat as
`eais-book` above):

```
python -c "from eais_scheduling_agent.http_api import run; run()"
```

Two endpoints, both exercising the exact same `SchedulingAgentCore` the
CLI uses -- no separate decision logic:

- `POST /bookings` -- body `{"sector": "clinic", "text": "...", "llm": false}`
  (`llm` optional, defaults to `false`). Returns `{"status": "CONFIRMED",
  "message": "..."}` or `{"status": "PENDING_APPROVAL", "reason": "..."}`.
  An unrecognized sector returns `404`; a malformed body returns `400`.
- `GET /audit` -- returns `{"records": [...]}`, the same JSON Lines
  audit records `eais-book` writes, read back as a JSON array.

```
$ curl -X POST http://127.0.0.1:5000/bookings \
    -H "Content-Type: application/json" \
    -d '{"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"}'
{"message":"Confirmed: John Doe with Dr. A at 2026-08-05 10:00:00.","status":"CONFIRMED"}
```

**One behavioral difference from the CLI, worth knowing:** the server
keeps one shared in-memory booking store for as long as it runs, so two
`POST /bookings` calls to the *same* running server can conflict with
each other. The CLI cannot show this -- each `eais-book` invocation is a
separate process with a fresh, empty store, so two separate CLI calls
never conflict regardless of what they book. See
`docs/superpowers/specs/2026-08-05-http-interface-design.md` for why.
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml eais_scheduling_agent/http_api.py README.md
git commit -m "Add eais-book-server console script and README section"
```

---

### Task 4: Doc accuracy — `ARCHITECTURE.md`, `DESIGN.md`, `PLAN.md`

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `DESIGN.md`
- Modify: `PLAN.md`

No new code in this task — this is the project's established "docs stay honest and accurate" discipline (see `PLAN.md`'s definition of done), applied to the two claims this feature makes newly inaccurate, plus an honest record that the original cut-list decision was revisited.

- [ ] **Step 1: Correct `ARCHITECTURE.md`'s extension-point map**

In `ARCHITECTURE.md`, Section 3 ("Extension point map") currently has no row about entry points naming sectors — this plan does not add one (the table is about sector *extension*, which this feature doesn't touch — `core/` is untouched, confirmed by Task 2's constraint). Instead, add one sentence at the end of Section 1 ("Component diagram"), after the existing "Dependency direction is one-way" paragraph:

```markdown
Sector-naming knowledge (which concrete `SkillPack` class backs each
manifest's `skill_pack` string) lives in `eais_scheduling_agent/wiring.py`,
shared by both entry points (`cli.py`, and the optional `http_api.py`) --
neither entry point defines this mapping independently.
```

- [ ] **Step 2: Correct `DESIGN.md`'s "one place allowed to name sectors" claim**

In `DESIGN.md` Section 1 ("The skill-pack resolution mechanism"), find the paragraph containing:

```
That happens to be `cli.py` (T14), which is why `cli.py`'s own module docstring states plainly: "This is the one place in the project allowed to name sectors."
```

Replace it with:

```
That happens to be `wiring.py` (originally part of `cli.py` at T14, extracted when the optional HTTP interface was added), which both `cli.py` and `http_api.py` consume rather than defining independently -- see `wiring.py`'s own module docstring.
```

In `DESIGN.md` Section 6 ("The CLI wiring layer (T14)"), find:

```
`eais_scheduling_agent/cli.py` is, by design, **the one place in the
project allowed to name a sector.**
```

Replace it with:

```
`eais_scheduling_agent/cli.py` was originally, by design, **the one
place in the project allowed to name a sector** (T14). That knowledge
now lives in `wiring.py`, shared with the optional HTTP interface added
afterward -- `cli.py` remains the CLI-specific wiring (argument parsing,
stdout rendering), but no longer defines the sector-naming mapping
itself.
```

- [ ] **Step 3: Add an honest addendum to `PLAN.md`**

In `PLAN.md`, after Section 7 ("Plan vs. actual"), add a new section:

```markdown
## 8. Post-submission-prep addendum: the HTTP interface, reconsidered

Section 6's cut list put the HTTP interface first to cut, and Stage B
shipped with the CLI only, matching that decision. It was revisited
afterward and built anyway -- not a silent scope change: the brief
itself treats this as fully optional (§5.2 lists it as one of two
acceptable interface choices, `AC`‑unconstrained), so building it adds
demonstrable engineering judgement (an explicit build-vs-skip trade-off,
argued and documented in `docs/superpowers/specs/2026-08-05-http-interface-design.md`)
without displacing anything the definition of done in Section 5 actually
requires. Per the brief's own stance: no penalty for a plan that
changed, only for pretending it did not -- this section is that record.
```

- [ ] **Step 4: Run the full test suite one more time (docs-only changes, but confirm nothing else drifted)**

Run: `python -m pytest`
Expected: `PASS`, same count as Task 2's end state.

- [ ] **Step 5: Commit**

```bash
git add ARCHITECTURE.md DESIGN.md PLAN.md
git commit -m "Correct sector-naming claims in ARCHITECTURE.md/DESIGN.md; record the HTTP interface decision in PLAN.md"
```

---

## After all four tasks

- [ ] Push the branch: `git push -u origin feature/http-interface`
- [ ] Open a PR against `develop` (same workflow as every other change in this repo), and wait for both CI checks (Python 3.11, Python 3.12) before considering it mergeable. **Do not merge without Ahmed's explicit go-ahead**, same as every other PR this session.
