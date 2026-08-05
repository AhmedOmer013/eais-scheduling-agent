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

`--llm` swaps the offline parser for an LLM-backed one (via a local
[Ollama](https://ollama.com) server, model `llama3.2` by default). It
requires a locally running Ollama instance to do anything beyond what
offline mode already does — **it is not required for the default
path**, and `--llm` is safe to pass even without Ollama running: on any
failure (Ollama not running, unreachable, or returning something
unusable) it automatically and silently falls back to the same offline
parser used by default, never raising and never blocking. Verified in
this environment (no Ollama installed here):

```
$ python -m eais_scheduling_agent.cli clinic "Dr. A today at 11am, patient Jane Roe" --llm
Confirmed: Jane Roe with Dr. A at 2026-08-05 11:00:00.
```

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
  audit records `eais-book` writes, read back as a JSON array. Reads the
  whole file from disk on every call, with no pagination or auth; since
  `audit.jsonl` is shared with the CLI's default output file and
  persists across server restarts, this can show records from previous
  server runs and separate `eais-book` CLI invocations too -- even
  though the in-memory booking store itself resets on every restart, so
  `/audit` can list confirmed bookings the store has no memory of.

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
============================= 251 passed in 8.01s =============================
```

`pytest`'s configuration (`[tool.pytest.ini_options]` in
`pyproject.toml`) restricts test collection to `tests/`, in verbose
mode (`-v`) by default — no extra flags needed. **No network access is
required**: every test exercises `LLMIntake` (T13) through an injected
fake HTTP client rather than a real socket (confirmed directly — no test
file references `urllib`, `socket`, `requests`, or `http.client`; one
test, `tests/test_offline_intake.py`, explicitly asserts none of those
modules are imported by the offline path at all).

## Known gaps

(Kept consistent with the fuller account in `HANDOVER.md`.)

- **`RestaurantSkillPack` table assignment** is static and deterministic
  (smallest fitting table), not a live-availability search — the
  interface calls `slot_rules()` before the store's conflict check, so
  the skill pack has no visibility into current occupancy at
  assignment time. Documented as an accepted design trade-off in the
  class's own docstring, not a bug. See `DESIGN.md` §3.
- **No local LLM runtime is installed in this environment.** `LLMIntake`'s
  real Ollama-calling code (`OllamaHTTPClient`) is exercised in
  production but not against a real network call in this environment or
  CI; its fallback and validation logic are fully tested against
  injected fakes, and its failure-handling contract is exercised
  end-to-end (see `--llm` above).

## Further reading

- `RESEARCH.md`, `PLAN.md`, `ARCHITECTURE.md` — the Stage A documents
  behind this project (research, plan, and planned design).
- `DESIGN.md` — as-built design decisions made during implementation,
  where reality required a call `ARCHITECTURE.md` didn't anticipate.
- `AI_USAGE.md` — how this project was built with AI assistance,
  including concrete cases where AI output was wrong and required
  correction.
- `HANDOVER.md` — current project state and the full known-gaps list.
