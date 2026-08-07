# EAIS Scheduling Agent

## What this is

A multi-sector appointment-booking agent — clinic and restaurant, today —
built around one sector-agnostic core (`SchedulingAgentCore`). A booking
request (free text, e.g. `"Dr. A today at 10am, patient John Doe"`) is
parsed into a structured request, checked against its sector's rules
(working hours, sector-specific constraints, double-booking), and either
auto-confirmed or escalated for human approval — with every decision
recorded to a JSON Lines audit trail. The core itself contains zero
sector-specific code: it depends only on abstract interfaces
(`SkillPack`, `IntakeService`, `ApprovalGate`, `BookingStore`,
`AuditTrail`), and adding a new sector means writing a new skill pack and
manifest, never touching the core. See `DESIGN.md` for exactly how that
works and `ARCHITECTURE.md` for the original design contract.

## Install

Requires **Python >= 3.10** (see `pyproject.toml`'s `requires-python`).
Verified on Python 3.14.4 in this environment.

Editable install (for development — code changes take effect without
reinstalling):

```
pip install -e .
```

Non-editable install (installs a real, self-contained package — this is
what a real deployment or a wheel build produces):

```
pip install .
```

Both are fully supported and both were verified directly for this task:
`pip install -e .` in this worktree, and `pip install .` into a fresh,
isolated virtual environment. Either one takes well under a minute on a
clean machine (no compiled extensions — the only runtime dependency is
`PyYAML`).

```
$ pip install -e .
...
Successfully installed eais-scheduling-agent-0.1.0
```

**Windows-specific note:** pip installs the `eais-book` console script
into your Python installation's `Scripts` directory, which is
**not on `PATH` by default** on Windows (pip prints a warning about this
during install). If `eais-book` is not found after installing, either:

- invoke it via `python -m eais_scheduling_agent.cli ...` instead (works
  identically, verified below), or
- invoke the installed script by its full path (pip's install-time
  warning tells you exactly where it went, e.g.
  `C:\Users\<you>\AppData\Roaming\Python\Python3XX\Scripts\eais-book.exe`).

Both were verified working in this environment; `python -m
eais_scheduling_agent.cli` is the more portable of the two and is used
in the examples below.

## Run

Offline mode (the default — a deterministic, regex-based parser, no
network access) requires nothing further than the install above:

```
eais-book <sector> <text> [--llm] [--audit-file PATH] [--manifest-dir DIR]
```

- `sector` — `clinic` or `restaurant` (must match a manifest file under
  `eais_scheduling_agent/manifests/`).
- `text` — free-text booking request.
- `--llm` — use LLM-backed intake instead of the offline parser (see
  below).
- `--audit-file PATH` — JSON Lines audit file to append to (default:
  `./audit.jsonl`, already git-ignored).
- `--manifest-dir DIR` — directory of sector manifests (default: the
  package's own bundled manifests).

Verified, real terminal output from this worktree:

```
$ python -m eais_scheduling_agent.cli clinic "Dr. A today at 10am, patient John Doe"
Confirmed: John Doe with Dr. A at 2026-08-05 10:00:00.

$ python -m eais_scheduling_agent.cli restaurant "table for 4 today at 6pm, customer Jane Smith"
Confirmed: Jane Smith, party of 4, at 2026-08-05 18:00:00.

$ python -m eais_scheduling_agent.cli clinic "Dr. A today at 6am, patient John Doe"
Pending approval: outside working hours: requested 06:00, hours are 09:00-17:00
```

(The bundled `ClinicSkillPack` defaults to two practitioners, `Dr. A`
and `Dr. B`; a request naming any other practitioner is a valid but
unconfirmed request — it comes back `Pending approval: unknown
practitioner: '...'` rather than an error, exit code `0` either way.
Only a request that names a sector with no manifest at all, e.g.
`eais-book veterinary ...`, is a hard error — printed to stderr with a
non-zero exit code.)

Every run appends exactly one line to the audit file (default
`./audit.jsonl`), recording the input text, which skill pack handled it,
the extracted fields, which rules were evaluated and how they came out,
the decision, and a UTC timestamp.

### `--llm` mode

`--llm` swaps the offline parser for an LLM-backed one, talking to any
OpenAI-compatible API — a local [Ollama](https://ollama.com) server by
default (model `llama3.2`), or a hosted server (e.g. vLLM) if configured
-- see "Configuring the LLM backend" below. It requires a reachable LLM
server to do anything beyond what offline mode already does — **it is
not required for the default path**, and `--llm` is safe to pass even
without one running: on any failure (unreachable, timed out, or
returning something unusable) it automatically and silently falls back
to the same offline parser used by default, never raising and never
blocking. Verified in this environment (no local model server running
here):

```
$ python -m eais_scheduling_agent.cli clinic "Dr. A today at 11am, patient Jane Roe" --llm
Confirmed: Jane Roe with Dr. A at 2026-08-05 11:00:00.
```

### Configuring the LLM backend

`--llm` (CLI) and `"llm": true` (HTTP API) both talk to whatever
OpenAI-compatible server these environment variables point at. Unset,
they default to a local Ollama server -- set them to reach a different
local model or a hosted one instead:

| Variable | Default | Meaning |
|---|---|---|
| `EAIS_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible API root; `/chat/completions` is appended automatically |
| `EAIS_LLM_MODEL` | `llama3.2` | Model name sent in each request |
| `EAIS_LLM_API_KEY` | unset | Sent as `Authorization: Bearer <key>` if set; omitted entirely otherwise |
| `EAIS_LLM_TIMEOUT` | `60.0` | Per-request timeout in seconds |

**Free-tier hosted option: [Groq](https://console.groq.com).** No credit
card required. Sign up, create an API key under "API Keys", then:

```
export EAIS_LLM_BASE_URL="https://api.groq.com/openai/v1"
export EAIS_LLM_MODEL="llama-3.3-70b-versatile"
export EAIS_LLM_API_KEY="gsk_..."
```

Verified against the real Groq API (see `EXTENSIONS.md` for the
User-Agent fix this required, and "Evaluation" below for accuracy/latency
numbers from a real run against it).

This is not part of the original assessment brief's scope -- see
`EXTENSIONS.md`.

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

Nine endpoints, all exercising the exact same `SchedulingAgentCore` the
CLI uses -- no separate decision logic (see `eais_scheduling_agent/http_api.py`
for the authoritative list and exact request/response shapes):

- `POST /bookings` -- body `{"sector": "clinic", "text": "...", "llm": false}`
  (`llm` optional, defaults to `false`). Returns `{"status": "CONFIRMED",
  "message": "..."}`, `{"status": "PENDING_APPROVAL", "reason": "..."}`, or
  `{"status": "NEEDS_CLARIFICATION", "reason": "..."}` (intake couldn't
  extract enough from the text). An unrecognized sector returns `404`; a
  malformed body returns `400`.
- `GET /audit` -- returns `{"records": [...]}`. Each sector's requests are
  now recorded to a genuinely separate file (`audit.clinic.jsonl` /
  `audit.restaurant.jsonl`, derived from the server's own `--audit-file`
  base), completely separate from the CLI's own `audit.jsonl` -- the CLI's
  audit trail is unaffected by anything the server writes. An optional
  `?sector=clinic|restaurant` query param restricts the response to that
  sector's file; omitting it returns the merged, chronologically-sorted
  view across both sectors, kept for backward compatibility. Reads the
  whole file from disk on every call, with no pagination or auth; since
  each sector's audit file persists across server restarts, this can show
  records from previous server runs -- even though the in-memory booking
  store itself resets on every restart, so `/audit` can list confirmed
  bookings the store has no memory of.
- `GET /pending` -- returns `{"items": [...]}`, the human accept/reject
  queue (optionally filtered with `?sector=clinic|restaurant`).
- `POST /pending/<id>/accept` -- accepts a queued item as a real confirmed
  booking. See "Web dashboard" below for when this can 422.
- `POST /pending/<id>/reject` -- discards a queued item, logged to the
  audit trail.
- `GET /config` / `POST /config` -- view or change the LLM backend config
  at runtime.
- `GET /config/clinic` / `POST /config/clinic` and `GET /config/restaurant`
  / `POST /config/restaurant` -- view or change that sector's slot rules
  (practitioners/tables, durations/capacities, working hours) at runtime.

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
This reasoning assumes single-request-at-a-time handling; the dev server
runs threaded by default and the store has no internal locking, so two
truly concurrent requests are not guaranteed to serialize correctly.

## Web dashboard (optional)

With the server running (see above), open `http://127.0.0.1:5000/` in a
browser. Five tabs:

- **Book** -- make a booking. Three possible outcomes, each styled
  distinctly: Confirmed (green), sent for human review (terracotta,
  violation/conflict cases), or needs clarification (rose, when intake
  couldn't extract enough from the text -- an inline message, not queued
  anywhere).
- **Pending** -- requests with a complete, understood booking that needs
  a human's judgment call (unknown practitioner, over capacity, slot
  conflict). Reject always discards it, logged to the audit trail. Accept
  replays the sector's own rule check, so it can only persist an item as
  a real confirmed booking if that check now passes: a genuinely
  computable violation (e.g. outside working hours) or a since-resolved
  slot conflict can be accepted as-is, but an unknown-practitioner or
  over-capacity item still fails validation and comes back `422` --
  add the practitioner/table first via that sector's Slot rules card
  (Audit: Clinic / Audit: Restaurant tab), then Accept again. Both
  Accept and Reject are logged to the audit trail. Survives a server
  restart (file-backed, unlike the in-memory booking store).
- **Audit: Clinic** / **Audit: Restaurant** -- genuinely separate audit
  files per sector (`audit.clinic.jsonl` / `audit.restaurant.jsonl`), each
  with a "Slot rules" card above the audit table showing that sector's
  practitioners (clinic) or tables (restaurant) and working hours, with
  an Edit form to add a practitioner/table, change a duration/capacity
  or the working hours, and a Delete button on each entry to remove it.
  The last remaining practitioner/table can't be deleted -- an empty
  sector would make every booking an automatic violation.
- **Config** -- view or change the LLM backend (`base_url`/`model`/
  `api_key`/`timeout`) at runtime, without restarting the server. The API
  key is never sent back to the browser as its raw value, only whether
  one is currently set.

Not part of the assessment brief's scope -- see `EXTENSIONS.md`.

## Run everything with one command (optional)

```
./run.ps1     # Windows / PowerShell
./run.sh      # macOS / Linux / Git Bash
```

Installs the `http` extra if it's missing, loads `EAIS_LLM_*` from a
local `.env` if present (without overriding anything already set in your
shell -- see "Configuring the LLM backend" above for what to put in it),
starts the server at `http://127.0.0.1:5000`, and opens the dashboard in
your default browser. Ctrl+C stops it. Equivalent to manually running
`pip install -e ".[http]"` then the server command above -- this just
collapses both into one step. Not part of the assessment brief's scope.

## Evaluation: LLM intake accuracy & efficiency (optional)

`scripts/eval_llm_intake.py` runs the project's existing 40-example
labeled fixture set (`training/clinic_examples.jsonl` +
`training/restaurant_examples.jsonl` -- reused, not a new synthetic
dataset) through both `OfflineIntake` and the real Groq-backed
`LLMIntake`, end to end through a real `SchedulingAgentCore`, and reports
field-extraction accuracy, latency, and CONFIRMED-rate metrics. Not part
of the assessment brief's scope; see `EXTENSIONS.md`.

Results from a real run against Groq (`llama-3.3-70b-versatile`),
2026-08-06 -- full detail (per-example results, methodology notes) in
the accompanying Excel workbook, `eais_llm_intake_evaluation_2026-08-06.xlsx`:

| Metric | Result |
|---|---|
| Requests used | 40 (4% of Groq's free-tier daily allowance -- well under the 30% budget) |
| LLM path used (not offline fallback) | 100% |
| Field extraction precision / recall / F1 | 89.6% / 95.2% / 92.3% |
| Relative-date resolution accuracy | 92% (25 scored examples) |
| Average / P95 latency | 0.31s / 0.59s |
| CONFIRMED rate: offline vs. LLM | 12.5% vs. 12.5% (dataset skews toward deliberately ambiguous/incomplete requests, by design, to exercise the approval gate) |

Reproduce with `python scripts/eval_llm_intake.py` (needs
`EAIS_LLM_API_KEY` set; makes real, rate-limited network calls -- never
run in CI).

## Run tests

```
pytest
```

or, if `pytest`'s console script is also not on your `PATH` (same
Windows caveat as `eais-book` above — both scripts install to the same
directory):

```
python -m pytest
```

Both were run in this worktree; real output:

```
$ python -m pytest
...
============================ 305 passed in 13.64s =============================
```

`pytest`'s configuration (`[tool.pytest.ini_options]` in
`pyproject.toml`) restricts test collection to `tests/`, in verbose
mode (`-v`) by default — no extra flags needed. **No network access is
required**: no test opens a real network socket. Most tests exercise
`LLMIntake` (T13) through an injected fake `HTTPClient` callable rather
than a real one; `OpenAICompatibleHTTPClient`'s own request-building
logic (`tests/test_llm_intake.py::TestOpenAICompatibleHTTPClient`) is
instead tested by monkeypatching `urllib.request.urlopen`, which also
never opens a real socket. Separately, `tests/test_offline_intake.py`
explicitly asserts that `urllib`, `socket`, `requests`, and
`http.client` are not imported by the offline path at all.

## Known gaps

(Kept consistent with the fuller account in `HANDOVER.md`.)

- **`RestaurantSkillPack` table assignment** is static and deterministic
  (smallest fitting table), not a live-availability search — the
  interface calls `slot_rules()` before the store's conflict check, so
  the skill pack has no visibility into current occupancy at
  assignment time. Documented as an accepted design trade-off in the
  class's own docstring, not a bug. See `DESIGN.md` §3.
- **No local LLM runtime is installed in this environment.** `LLMIntake`'s
  real network-calling code (`OpenAICompatibleHTTPClient`) is exercised
  in production but not against a real network call in this environment
  or CI; its request-building logic is directly tested via a
  monkeypatched `urlopen`, its fallback and validation logic are fully
  tested against injected fakes, and its failure-handling contract is
  exercised end-to-end (see `--llm` above).

## Further reading

- `RESEARCH.md`, `PLAN.md`, `ARCHITECTURE.md` — the Stage A documents
  behind this project (research, plan, and planned design).
- `DESIGN.md` — as-built design decisions made during implementation,
  where reality required a call `ARCHITECTURE.md` didn't anticipate.
- `AI_USAGE.md` — how this project was built with AI assistance,
  including concrete cases where AI output was wrong and required
  correction.
- `HANDOVER.md` — current project state and the full known-gaps list.
