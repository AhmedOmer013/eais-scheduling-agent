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
variables, read in one place (`wiring.build_llm_client()`):

- `EAIS_LLM_BASE_URL` (default: `http://localhost:11434/v1`)
- `EAIS_LLM_MODEL` (default: `llama3.2`)
- `EAIS_LLM_API_KEY` (default: unset -- no auth header sent)
- `EAIS_LLM_TIMEOUT` (default: `60.0` seconds)

Both a local Ollama server and a hosted vLLM server (the intended use: a
Qwen-72B model, reached over Tailscale) are the same code path -- only
the configuration differs. See
`docs/superpowers/specs/2026-08-05-configurable-llm-backend-design.md`
for the full design rationale.

### 2. Web UI *(planned, not yet built)*

A browser-based front end for making booking requests, built on top of
the existing `POST /bookings` / `GET /audit` HTTP API
(`eais_scheduling_agent/http_api.py`). Will get its own design spec and
plan before implementation, same as everything else in this repo.

### 3. Playwright end-to-end tests *(planned, not yet built)*

Drives the web UI above through a real browser once it exists.
