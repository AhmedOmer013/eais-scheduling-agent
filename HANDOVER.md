# HANDOVER.md

## What this is

A multi-sector appointment-booking agent (clinic + restaurant) built
around one sector-agnostic core (`SchedulingAgentCore`), proven by two
independently-verified properties: **FR1** — the core contains zero
sector-named identifiers (grep- and import-graph-checked, `tests/
test_r1_proof.py`) — and **AC5** — adding the restaurant sector touched
only a manifest file and a new skill-pack package, zero diffs to `core/`
(verified twice, independently, before merge).

## Current state

All 15 `PLAN.md` tasks are complete and merged to `develop` via reviewed
PRs. Both sectors run end-to-end through the `eais-book` CLI, in either
deterministic-offline or LLM-backed (Ollama, with automatic fallback)
intake mode. Full test suite: 235+ tests, CI-green on Python 3.11/3.12.

## How to run it

`pip install -e .`, then e.g.:
`eais-book clinic "Dr. A today at 10am, patient John Doe"`

See `README.md` for the full install/run/test walkthrough.

## Known gaps

- `RestaurantSkillPack` assigns tables deterministically (smallest
  fitting table) without live-availability visibility, since the
  interface calls `slot_rules()` before `check_conflict()`. Documented in
  the class's own docstring as an accepted simplification, not a bug.
- No local LLM runtime is installed in this environment; `LLMIntake`'s
  Ollama-calling code path is untested against a real model (its
  fallback and parsing logic are fully tested against injected fakes).

## AI-assisted development

Built with Claude Code in an agentic, task-by-task workflow with
independent review per task. See `AI_USAGE.md` for concrete cases where
AI output was wrong and required correction.
