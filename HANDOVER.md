# HANDOVER.md

## What this is

A multi-sector appointment-booking agent (clinic + restaurant) built
around one sector-agnostic core (`SchedulingAgentCore`), proven by two
verified properties: **FR1** — the core has zero sector-named
identifiers (checked by `tests/test_r1_proof.py`) — and **AC5** — adding
the restaurant sector touched only a manifest and a skill-pack package,
zero diffs to `core/`.

## Current state

All 15 `PLAN.md` tasks are complete and merged to `develop` via reviewed
PRs. Both sectors run end-to-end through the `eais-book` CLI, in either
deterministic-offline or LLM-backed (Ollama, with automatic fallback)
intake mode. An optional HTTP interface (`eais-book-server`) was added
afterward — same core, same rules, over HTTP instead of a command line.
It keeps one shared store for its run, so it can catch a conflict across
two separate requests, unlike two separate CLI calls. 271 tests passing,
CI-green on Python 3.11/3.12.

## How to run it

`pip install -e .`, then e.g.:
`eais-book clinic "Dr. A today at 10am, patient John Doe"`

HTTP interface: `pip install -e ".[http]"`, then `eais-book-server`. Full
walkthrough for both in `README.md`.

## Known gaps

- `RestaurantSkillPack` assigns tables deterministically (smallest
  fitting table) without live-availability visibility, since the
  interface calls `slot_rules()` before `check_conflict()`. Documented in
  the class's own docstring as an accepted simplification, not a bug.
- No local LLM runtime is installed in this environment; `LLMIntake`'s
  Ollama-calling code path is untested against a real model (its
  fallback and parsing logic are fully tested against injected fakes).
- The HTTP dev server has no concurrency hardening — two simultaneous
  requests for the same slot may not serialize correctly. Out of scope
  to fix for this prototype.

## AI-assisted development

Built with Claude Code in an agentic, task-by-task workflow with
independent review per task. See `AI_USAGE.md` for concrete cases where
AI output was wrong and required correction.
