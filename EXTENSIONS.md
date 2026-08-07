# EXTENSIONS.md

This file exists to keep one thing unambiguous: everything described here
is **outside the scope of the `EAIS-HR-2159-TA-01` technical assessment
brief**, built afterward at the repo owner's explicit request, after being
told plainly that it goes against the brief's own scope guidance. It is
not referenced from `RESEARCH.md`, `PLAN.md`, or `ARCHITECTURE.md`, which
remain accurate, unmodified records of the brief-scoped submission.

## Why this file exists

The brief (§5.3) explicitly lists "real integrations" and "production
infrastructure" as out of scope, and separately instructs against building
"any user interface, dashboard or front end" (also §5.3), warning that
doing so "will not earn points and may cost you points for poor scoping."
The work below does some of exactly that. It was a deliberate, informed
choice, not an oversight -- and per this project's own established
practice of disclosing scope changes rather than hiding them (see
`PLAN.md` §8), it gets the same treatment here, in its own clearly
separated file.

## Extensions

### 1. Configurable LLM backend (local + hosted)

`LLMIntake`'s HTTP client (`eais_scheduling_agent/intake/llm.py`) now
speaks a generic OpenAI-compatible `/chat/completions` API instead of
being hardcoded to a local Ollama server. Configured via four environment
variables, read in one place (`wiring.resolve_llm_config()`). The web
dashboard (§2 below) can layer a runtime override on top of these for the
running server process, without touching the environment itself.

- `EAIS_LLM_BASE_URL` (default: `http://localhost:11434/v1`)
- `EAIS_LLM_MODEL` (default: `llama3.2`)
- `EAIS_LLM_API_KEY` (default: unset -- no auth header sent)
- `EAIS_LLM_TIMEOUT` (default: `60.0` seconds)

Both a local Ollama server and a hosted vLLM server (the intended use: a
Qwen-72B model, reached over Tailscale) are the same code path -- only
the configuration differs. See
`docs/superpowers/specs/2026-08-05-configurable-llm-backend-design.md`
for the full design rationale.

Known limitation: `EAIS_LLM_API_KEY`, if set, is sent as a bearer token
with no TLS enforcement and no redirect protection -- treat
`EAIS_LLM_BASE_URL` as a trusted endpoint, not one you'd point at an
untrusted or redirect-capable proxy. Full production hardening here is
explicitly out of scope for this prototype (see the top of this file).

### 2. Web UI

A single-page, server-rendered dashboard (`GET /`,
`eais_scheduling_agent/templates/dashboard.html`) with three panels:
making a booking, viewing the audit trail, and viewing/changing the LLM
backend config at runtime. Plain HTML/CSS/JS -- no build step, no JS
framework -- served by the same Flask app as the JSON HTTP API.

Model config is read-write: `GET /config` / `POST /config` let you view
and change `base_url`/`model`/`api_key`/`timeout` for the running server
process, taking effect on the very next booking request (no restart).
The API key is never returned to the browser as its raw value -- only
whether one is set. No authentication, consistent with every other
endpoint in this project.

Known limitation: because `POST /config` is unauthenticated (like every
other endpoint here) and can change `base_url`, anyone who can reach the
server's port can redirect LLM traffic -- and any `EAIS_LLM_API_KEY` set
in the environment will then be sent as a bearer token to that new
destination. The dev server binds to `127.0.0.1` only, containing this to
local access; a network-exposed deployment would need authentication
before this endpoint is safe. Separately: `POST /config` and
`POST /bookings` have no CSRF token. Today a cross-origin browser request
can't reach them in practice, because Flask's JSON-only body parsing
rejects the simple content types a plain HTML form can send, and a
cross-origin `fetch()` with a JSON content type triggers a CORS preflight
nothing here answers -- but this is an incidental side effect of
JSON-only parsing, not a deliberate control, and must not be relied on if
these endpoints' accepted content types ever widen.

**2026-08-07 redesign:** rebuilt around a tab bar (Book | Pending | Audit:
Clinic | Audit: Restaurant | Config) with a Warm Neutral visual style.
Two new capabilities: (1) `PENDING_APPROVAL` requests with a complete,
understood booking (unknown practitioner, over capacity, conflict) are
now queued in a file-backed `PendingRequestStore`
(`eais_scheduling_agent/pending.py`) for a human to accept or reject on
the new Pending tab -- accepting replays the exact
`slot_rules()`/`check_conflict()`/`persist()` sequence
`core/orchestrator.py` already uses internally, so acceptance can never
diverge from the core's own decision logic. One consequence of that
replay: `slot_rules()` still raises for an unknown practitioner or an
over-capacity table exactly as it does on the original request, so
Accept 422s for those two cases rather than persisting them as-is --
only a genuinely computable violation (e.g. outside working hours) or a
since-resolved conflict can actually be accepted. An unknown-practitioner
or over-capacity item needs its sector's Slot rules card (add the
practitioner/table, see (3) below) before Accept will succeed;
(2) requests where intake
couldn't extract enough (`missing required field(s): ...`) are now a
distinct `NEEDS_CLARIFICATION` response instead of being lumped in with
`PENDING_APPROVAL` -- there's no complete booking to review in that case,
just an inline message asking for more detail. The audit trail is also
now genuinely split into `audit.clinic.jsonl` / `audit.restaurant.jsonl`
(web server only -- the CLI's `audit.jsonl` is unaffected), with
`GET /audit`'s existing no-argument merged view kept for backward
compatibility. (3) Each sector's slot rules (clinic: practitioners and
their appointment duration; restaurant: tables and their seat capacity;
both: working hours) are now viewable and editable from that sector's
audit tab via `GET/POST /config/clinic` and `GET/POST /config/restaurant`
-- additive/corrective only (add a practitioner, change a duration for
clinic; add a table, change a capacity for restaurant), no deletion.
`ClinicSkillPack`/`RestaurantSkillPack` gained one read-only
property each (`practitioners`/`tables`, mirroring the existing
`working_hours` property); all mutation happens by constructing a new
skill pack instance and swapping it into the `skill_packs` dict
`create_app()` already holds, not by adding setters to those classes.

See `docs/superpowers/specs/2026-08-06-web-ui-design.md` for the original
design rationale and `docs/superpowers/specs/2026-08-07-dashboard-redesign-design.md`
for this redesign's.

### 3. Playwright end-to-end tests *(planned, not yet built)*

Drives the web UI above through a real browser.
