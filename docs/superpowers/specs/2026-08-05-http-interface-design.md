# HTTP interface — design

**Date:** 2026-08-05
**Status:** Approved by Ahmed, ready for implementation planning.

## Context

The brief (§5.2) lists the interface as a deliberately unspecified choice: "a single HTTP endpoint, a CLI, or both." The CLI (`eais-book`, T14) already satisfies this on its own — the HTTP interface is optional, not required by any acceptance criterion, and was in fact first on `PLAN.md`'s own cut-list ("HTTP interface — keep CLI only, since neither is required by any AC"). This spec exists because Ahmed asked for it anyway, having weighed that trade-off explicitly rather than by default.

Two constraints shape everything below:

1. **The project's own minimalism precedent.** `RESEARCH.md` argues directly against "a heavy dependency for a problem we could solve in two hundred lines," and `PyYAML` is deliberately the only runtime dependency today. Adding an HTTP layer should not quietly abandon that stance.
2. **The sector-naming discipline.** `cli.py`'s docstring states plainly: "This is the one place in the project allowed to name sectors." A second entry point breaks that claim unless the sector-naming knowledge is shared, not duplicated.

## Decisions

### 1. Extract shared wiring (`wiring.py`)

`cli.py._skill_packs()` (the `"clinic_v1"` / `"restaurant_v1"` → class mapping) and the render-time manifest re-read / confirmation-message rendering (`cli.py._load_manifest_for_render` + the body of `_render_confirmation`) move into a new module, `eais_scheduling_agent/wiring.py`:

```python
def build_skill_packs() -> Dict[str, SkillPack]: ...
def load_manifest_for_render(manifest_dir: str, sector: str) -> SectorManifest: ...
def render_confirmation(skill_pack: SkillPack, request: BookingRequest) -> str: ...
```

`cli.py` is refactored to call these instead of defining them inline. Its docstring's "one place allowed to name sectors" claim is corrected to: *`wiring.py` is the one shared place that names sectors; `cli.py` and `http_api.py` both consume it, neither defines it independently.* `ARCHITECTURE.md`'s extension-point map and `DESIGN.md` §1/§6 get the same one-line correction, since both currently state the older, now-inaccurate claim.

No behavior change for the CLI — this is a pure extraction, covered by the existing CLI test suite continuing to pass unmodified.

### 2. New module: `eais_scheduling_agent/http_api.py`

A Flask app factory:

```python
def create_app(manifest_dir: str = _DEFAULT_MANIFEST_DIR, audit_file: str = "audit.jsonl") -> Flask: ...
```

(`_DEFAULT_MANIFEST_DIR` is the same package-bundled-manifests default `cli.py` already uses — moved to `wiring.py` alongside the rest of the shared wiring, so both entry points default to the identical location.)

`create_app` builds **exactly one** `SchedulingAgentCore` at call time — using `wiring.build_skill_packs()`, `StandardApprovalGate()`, **one** `InMemoryBookingStore()` instance, and `JsonLinesAuditTrail(path=audit_file)` — and closes over it for the lifetime of the returned app. This is the one deliberate behavioral difference from the CLI: the CLI builds a fresh store per process (per-invocation), so two separate `eais-book` calls can never conflict with each other. A long-running Flask server is one continuous process, so keeping one shared store makes cross-request conflict detection real for as long as the server runs — the CLI cannot demonstrate this on its own, and the server now can. State resets only on server restart; this is in-memory, matching R7's "in-memory or SQLite is fine" and the project's existing persistence choice.

### 3. Endpoints

| Route | Method | Request | Response |
|---|---|---|---|
| `/bookings` | `POST` | JSON body: `{"sector": str, "text": str, "llm": bool (optional, default false)}` | `200` `{"status": "CONFIRMED", "message": "..."}` or `{"status": "PENDING_APPROVAL", "reason": "..."}` |
| `/audit` | `GET` | — | `200` `{"records": [...]}` — the JSON Lines audit file's records, parsed into a JSON array, in file order |

`POST /bookings` mirrors `cli.py.main()`'s flow exactly: build intake (`OfflineIntake()`, or `LLMIntake(fallback=OfflineIntake())` when `"llm": true`, wrapped in the same `_CachingIntake` pattern) → `core.handle(text, sector)` → on `CONFIRMED`, render via `wiring.render_confirmation`; on `PENDING_APPROVAL`, return `decision.reason` directly. No new decision logic — this is a thin transport wrapper around the existing core, identical in spirit to what `cli.py` already does.

### 4. Error mapping

`core.handle()` can raise `OrchestrationError` subclasses (from `core/orchestrator.py`). These are configuration faults, not booking outcomes (the module's own docstring: "a bad booking produces a `PENDING_APPROVAL` decision; a bad configuration raises") — so they map to HTTP error responses, not to the 200-level `{"status": ...}` shape above:

| Exception | HTTP status | Rationale |
|---|---|---|
| `UnknownSectorError` | `404` | Client asked for a sector with no manifest at all — a "resource not found." |
| `SectorDisabledError` | `400` | Valid sector, but declared `enabled: false` — a client-facing rejection of this specific request. |
| `InvalidManifestError` | `500` | The manifest exists but fails validation — a server-side configuration problem, not something the caller did wrong. |
| `UnknownSkillPackError` | `500` | The manifest names a skill pack the server has no entry for — likewise a server-side misconfiguration. |

All error responses: `{"error": "<exception message>"}`. A malformed request body (missing `sector`/`text`, wrong JSON shape) is validated before `core.handle()` is even called and returns `400 {"error": "..."}` directly.

### 5. Packaging: Flask as an optional extra

```toml
[project.optional-dependencies]
http = ["flask>=3.0"]
```

`pip install -e .` (CLI-only, the brief's baseline "clean machine, under 5 minutes" path) stays exactly as light as it is today. `pip install -e ".[http]"` is required only for anyone who wants to run the server. `http_api.py` is not imported by anything in the CLI-only path, so `flask` missing is never an error unless someone actually tries to use the HTTP interface.

A new console script, `eais-book-server`, wraps `create_app().run()`, mirroring `eais-book`'s own registration pattern in `pyproject.toml`.

### 6. Testing

Flask's built-in `app.test_client()` — no real sockets opened, consistent with the project's existing "no network access in tests" discipline (verified the same way `LLMIntake`'s tests already avoid real HTTP calls). New test file `tests/test_http_api.py` covers:

- `POST /bookings` confirms a valid clinic/restaurant request (happy path, both sectors).
- `POST /bookings` returns `PENDING_APPROVAL` with the gate's reason for an out-of-hours/over-capacity/missing-field request.
- `POST /bookings` with an unknown sector → `404`.
- `POST /bookings` with a malformed body → `400`.
- `GET /audit` returns the records written by prior requests against the same test client.
- **The one test that exercises the shared-store decision directly:** two `POST /bookings` calls through the *same* `test_client()` instance, same resource/time, second one comes back `PENDING_APPROVAL` due to a conflict — proving cross-request state actually works, not just that each request works in isolation.

### 7. Documentation updates

- `README.md` — new "Run the HTTP API" section (install extra, start command, one `curl`/example request), alongside the existing "Run"/"Run tests" sections.
- `ARCHITECTURE.md`, `DESIGN.md` — the `wiring.py` correction from Decision 1.
- `PLAN.md` — a short, honest addendum noting the HTTP interface was reconsidered and added after the original submission prep, contradicting §6's original cut-list — in keeping with the brief's own "no penalty for a plan that changed, only for pretending it did not" stance. Not a rewrite of the original plan-vs-actual section, an addition after it.

## Out of scope (explicitly, for this change)

- Authentication, HTTPS, CORS, rate limiting, or any other production-hardening — the brief explicitly excludes "authentication, user accounts, multi-tenancy or deployment" and "production infrastructure" (§5.3).
- A UI/dashboard of any kind — explicitly discouraged by the brief (§5.3) and separately declined by Ahmed for this project.
- Any new decision logic — the HTTP layer is a transport wrapper only; `core/` is untouched by this change (verified by the R1-proof test continuing to pass, since `http_api.py` and `wiring.py` both live outside `core/`).

## Workflow

Same as every other change in this repo: a feature branch off `develop`, its own tests, a PR against `develop`, both CI checks (Python 3.11/3.12) green before squash-merge. No exception made for this being an "optional" feature.
