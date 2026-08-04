# training/ — Intake examples

These are **synthetic, hand-authored example utterances**, not scraped data — see "Why synthetic, not scraped" below. They exist to support the natural-language intake step (FR4), not to train or fine-tune any model.

## Files

- `clinic_examples.jsonl` — 10 examples for the clinic sector
- `restaurant_examples.jsonl` — 10 examples for the restaurant sector

Each line is one JSON object:

| Field | Meaning |
|---|---|
| `id` | Stable identifier, referenceable from tests |
| `text` | The raw free-text input |
| `category` | What this example stresses — see Coverage below |
| `expected` | Best-effort structured fields a correct parse should produce (`null` where the field is genuinely absent/ambiguous in the input — that's intentional, not a gap to fill) |
| `notes` | Why this example exists and what it's testing |

## Coverage

Each set spans, deliberately:

- **happy_path** — fully specified, should parse and (usually) confirm cleanly
- **missing_field** — a required field absent; should surface as `PENDING_APPROVAL(missing_required_field)` from the approval gate, not a parser crash
- **ambiguous_time / ambiguous_patient** — vague or referential input ("morning", "my son") the parser should either resolve conservatively or flag, never guess silently
- **out_of_hours** — parses fine, gets rejected by the *approval gate*, not by intake — these examples exist to keep that boundary honest
- **over_capacity** — same boundary check, restaurant-specific
- **casual_shorthand** — terse, typo'd, or abbreviated phrasing, to stress-test parser robustness
- **preference_extra_field** — optional fields beyond the required set (seating preference, occasion) that a skill pack may or may not use
- **unsupported_action** (clinic only) — a reschedule request, to confirm intake doesn't silently misfile it as a fresh, unrelated booking

## How this is meant to be used

1. **Offline parser tests (FR4, AC7)** — feed `text` through the deterministic offline intake path and assert the output matches `expected` (or the documented degraded behavior for the harder `casual_shorthand` cases). Repeat runs on the same `text` must produce identical output — that determinism is what AC7 checks.
2. **LLM few-shot examples (FR4)** — a subset of these (particularly `happy_path` and `preference_extra_field`) are reasonable few-shot examples for the LLM intake prompt, to anchor its output schema.
3. **Approval-gate tests (FR5)** — `out_of_hours`, `over_capacity`, and `missing_field` categories are exactly the cases `PLAN.md` T7 needs test coverage for.

## Why synthetic, not scraped

Real booking/appointment conversations pulled from live sites would likely carry other people's personal data (names, phone numbers, patient details) — the opposite of good practice, and specifically the kind of risk the brief's own interview section (production-readiness: "personal data in logs") asks candidates to think about avoiding. Synthetic examples give full control over field coverage and edge cases without that risk, and without any copyright question over reproduced text. See `AI_USAGE.md` (Stage B) for how these were generated.
