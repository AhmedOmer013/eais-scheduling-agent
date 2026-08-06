# Web UI — design

**Date:** 2026-08-06
**Status:** Approved by Ahmed, ready for implementation planning.

## Scope note — read this first

**This sub-project is explicitly outside the `EAIS-HR-2159-TA-01` assessment brief's scope**, same as sub-project 1 (the configurable LLM backend, already merged). The brief's §5.3 explicitly excludes building "any user interface, dashboard or front end," warning it "will not earn points and may cost you points for poor scoping." Ahmed was told this plainly before asking to proceed. Per this project's established practice of disclosing scope changes rather than hiding them, this work is recorded in `EXTENSIONS.md` at the repo root — **not** woven into `RESEARCH.md`, `PLAN.md`, or `ARCHITECTURE.md`, which remain accurate records of the brief-scoped submission.

Extending that boundary one file further than sub-project 1 did: **`DESIGN.md` is also untouched by this sub-project.** `DESIGN.md` is the graded submission's as-built record for T1-T15; UI decisions belong in `EXTENSIONS.md`'s own narrative, not blended into that document.

## Context

Ahmed asked for a web UI with three surfaces: making a booking (input), viewing the audit trail, and viewing/changing the LLM backend config. The project currently has a CLI and a JSON HTTP API (`POST /bookings`, `GET /audit`) — no UI, no templates, no static assets.

Two decisions were made explicit before designing:
1. **Model config is read-write, not read-only.** The UI can change the effective LLM backend (base URL, model, API key, timeout) for the running server process, taking effect on the very next booking request — no restart. This is new backend capability; today `wiring.build_llm_client()` only ever reads env vars.
2. **No authentication.** Consistent with every other endpoint in this project (including `GET /audit`, which already has none) — this is a local prototype, not something exposed publicly, and auth is explicitly out of scope everywhere else in this project. The API key field is masked in the UI as a basic precaution, not a security boundary.

## Decisions

### 1. Split `wiring.build_llm_client()` into a config-resolution step and a construction step

New `wiring.resolve_llm_config() -> dict`:

```python
def resolve_llm_config() -> dict:
    """Resolve EAIS_LLM_* env vars (or their defaults) into a plain dict.

    Returns {"base_url": str, "model": str, "api_key": Optional[str],
    "timeout": float} -- the same values build_llm_client() used to
    construct a client from directly, now exposed as data so a caller
    (the new /config endpoints) can inspect or layer overrides on top
    without needing a constructed OpenAICompatibleHTTPClient to peek
    inside of.
    """
```

`build_llm_client()` becomes:

```python
def build_llm_client() -> HTTPClient:
    config = resolve_llm_config()
    return OpenAICompatibleHTTPClient(**config)
```

No behavior change for `cli.py`, which keeps calling `build_llm_client()` exactly as today. `resolve_llm_config()`'s env-var-reading logic (including the `EAIS_LLM_TIMEOUT` malformed-value fallback) moves verbatim out of the old `build_llm_client()` body.

### 2. Runtime config override, held in `http_api.py`

```python
class _RuntimeLLMConfig:
    """In-memory override for the LLM backend config, settable via
    POST /config. Each field is None until explicitly set, meaning
    "no override -- use resolve_llm_config()'s value for this field."
    Lives for the server process's lifetime, same scope as the shared
    store/gate/audit `create_app()` already holds.
    """
    def __init__(self) -> None:
        self.base_url: Optional[str] = None
        self.model: Optional[str] = None
        self.api_key: Optional[str] = None
        self.timeout: Optional[float] = None

    def effective(self) -> dict:
        """Merge this override on top of resolve_llm_config()'s values."""
        base = wiring.resolve_llm_config()
        return {
            "base_url": self.base_url if self.base_url is not None else base["base_url"],
            "model": self.model if self.model is not None else base["model"],
            "api_key": self.api_key if self.api_key is not None else base["api_key"],
            "timeout": self.timeout if self.timeout is not None else base["timeout"],
        }
```

`create_app()` constructs one `_RuntimeLLMConfig()` instance, closed over by the new routes and by `post_booking()`'s `use_llm` branch (which now builds `OpenAICompatibleHTTPClient(**runtime_config.effective())` directly instead of calling `wiring.build_llm_client()`).

### 3. Two new routes

- **`GET /config`** — returns `runtime_config.effective()`, with `api_key` replaced by `api_key_set: bool` (`True` if the effective value is non-`None`/non-empty). The raw key is never sent back to the browser, even once saved.
- **`POST /config`** — body may include any subset of `base_url`, `model`, `api_key`, `timeout`. A field absent from the body is left untouched. For the three string fields (`base_url`, `model`, `api_key`): a non-empty string value sets that override; `""` (empty string) clears it back to env-var/default. For `timeout` (numeric): any JSON number (int or float) sets the override; JSON `null` clears it back to env-var/default; a non-numeric value (including a string) returns `400`, matching the project's existing "clear error, no traceback" discipline.

### 4. Frontend: one page, three panels, no build step

`GET /` serves `eais_scheduling_agent/templates/dashboard.html` (Flask's default template/static discovery — no new configuration needed, since `Flask(__name__)` in `http_api.py` already resolves relative to that package). Three sections:

- **Make a booking** — sector `<select>` (clinic/restaurant), free-text `<textarea>`, "use LLM" checkbox, submit button. On submit, `fetch()`s `POST /bookings`, displays the returned `CONFIRMED`/`PENDING_APPROVAL` result inline.
- **Audit trail** — a table populated by `fetch()`-ing `GET /audit` on page load and on a "Refresh" button click.
- **Model config** — form fields for base URL / model / API key (`type="password"`, placeholder text like "(already set — leave blank to keep)" when `api_key_set` is true) / timeout, populated from `GET /config` on load, saved via `POST /config` on submit.

One `eais_scheduling_agent/static/app.js` (plain JS, no framework) implements the three `fetch()` flows. One `eais_scheduling_agent/static/style.css` for basic, functional styling — clean and readable, not a design system.

### 5. Testing

Flask's `test_client()`, same pattern as every other HTTP test in this project — no real sockets. New coverage: `GET /config` returns defaults with `api_key_set: false` when nothing is configured; `POST /config` sets a field and a subsequent `GET /config` reflects it; `POST /config` with an empty string clears an override; `POST /config` with a malformed `timeout` returns `400`; `GET /` returns `200` and contains the three expected form/section markers (a smoke test, not a JS behavior test). **Actual browser-driven interaction (typing, clicking, seeing the DOM update) is sub-project 3's job (Playwright), deliberately not duplicated here.**

## Out of scope (explicitly, for this sub-project)

- Authentication of any kind (see "No authentication" above).
- Persisting the runtime config override across a server restart (in-memory only, same lifetime as the existing shared store/gate/audit).
- Any styling framework, build step, or JS framework — plain HTML/CSS/JS only.
- Playwright tests (sub-project 3, comes after this one exists).

## Workflow

Same as every other change in this repo: a feature branch off `develop` (`feature/web-ui`), its own tests, a PR, both CI checks green before squash-merge, no merge without Ahmed's explicit go-ahead.
