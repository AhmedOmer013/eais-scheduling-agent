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

See `docs/superpowers/specs/2026-08-06-web-ui-design.md` for the full
design rationale.

### 3. Playwright end-to-end tests *(planned, not yet built)*

Drives the web UI above through a real browser.
