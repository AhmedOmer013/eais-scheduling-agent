# DESIGN.md — as-built design decisions

This is not `ARCHITECTURE.md` again. `ARCHITECTURE.md` is the *planned*
design from Stage A — the contract the implementation is held against.
This document records what actually got decided *during* implementation
(T1–T14), especially the calls `ARCHITECTURE.md` didn't anticipate and
had to be made by a real task, against real code, under a real
constraint. Sourced primarily from the SDD ledger
(`.superpowers/sdd/PLAN/progress.md`), cross-checked against the code
itself.

## 1. The skill-pack resolution mechanism (T6)

This is the load-bearing idea behind FR1 ("the core contains zero
sector-named identifiers") and AC1, so it is worth being precise about
the exact mechanism rather than the slogan.

`SchedulingAgentCore.__init__` (`eais_scheduling_agent/core/orchestrator.py`)
takes a constructor argument:

```python
skill_packs: Mapping[str, SkillPack]
```

A `SectorManifest`'s `skill_pack` field (e.g. `"clinic_v1"`) is an
*opaque string* as far as the core is concerned — the core never
contains the literal string `"clinic_v1"`, never imports
`ClinicSkillPack`, and never branches on what the string says. All it
does with that string is a single dictionary-style lookup:

```python
def _resolve_skill_pack(self, identifier: str) -> SkillPack:
    try:
        return self._skill_packs[identifier]
    except KeyError as exc:
        raise UnknownSkillPackError(...)
```

That lookup is deliberately a single subscript, not an `in` check
followed by a subscript — a `Mapping` implementation that constructs
packs lazily inside `__getitem__` would otherwise do that work twice.

The type is `Mapping`, not `dict`, specifically so that *how* the
identifier-to-instance association is built is entirely the caller's
business. A plain `dict` (which is what `wiring.py`'s
`build_skill_packs()` returns) is the simple, static case; nothing stops
a future caller from supplying a `Mapping` that builds packs lazily,
reads them from a plugin registry, or does anything else — the core's
contract is satisfied by any object supporting `__getitem__`.

The person who is allowed to *build* that mapping — i.e. to write the
literal strings `"clinic"`, `"clinic_v1"`, `ClinicSkillPack` in the same
place — is whoever assembles the collaborators for `SchedulingAgentCore`.
That assembly now lives in `wiring.py` (originally part of `cli.py` at
T14, extracted when the optional HTTP interface was added), consumed
identically by both entry points (`cli.py` and `http_api.py`) when they
construct `SchedulingAgentCore` -- see `wiring.py`'s own module
docstring. Every other component (T6 through T13) is built and tested
against `core.interfaces` / `skillpacks.base` alone, and the R1-proof
test (T11, see §4 below) is what turns "the core never imports a
concrete skill pack" from a claim into something continuously checked.

The practical effect: adding a third sector is "write a new
`SkillPack` subclass, write a new manifest, add one entry to a dict in
`wiring.py`" — verified concretely by T10's AC5 checkpoint (adding
restaurant touched zero lines in `core/`, `manifests/manifest.py`, or
`skillpacks/base.py`, confirmed independently twice: once via `git diff
--stat`, once by the reviewer reading the diff file directly).

## 2. `SlotInfo`'s evolution (T4 → T9 → T10)

`SlotInfo` (`eais_scheduling_agent/skillpacks/base.py`) is the frozen
dataclass a skill pack's `slot_rules(request)` returns to describe the
resource footprint a booking occupies. It did not arrive in its final
shape; it grew in two deliberate, coordinated steps as later tasks
discovered requirements the earlier ones had no way to know about.

**T4 (original shape):** `{duration_minutes, capacity}` only. At the
time T4 shipped this, no consumer needed anything else — `ClinicSkillPack`
(T5) didn't exist yet, and neither did any notion of conflict detection.
This was a considered, minimal shape, not an oversight: T4's own report
explicitly left `capacity`'s restaurant semantics (party-size demand vs.
table-size supply) as an open question for whichever task added a
second sector, rather than guessing at a shape optimized for a sector
that didn't exist yet.

**T9 (widened, `resource_key` + `start` added, no defaults):** T9 needed
to build `InMemoryBookingStore.check_conflict()` — and a conflict check
needs two things `SlotInfo` didn't carry: *which* resource a booking
occupies (so two bookings on different practitioners/tables don't
falsely conflict) and *when* it starts (so the interval math has an
anchor). This was flagged as a known gap by T6's own report before T9
started ("a deliberate decision needed before T9 starts, not
improvised inside it") specifically so it would be a scoped, reviewed
change rather than something improvised under time pressure inside
T9's actual task. T9 is the first task explicitly authorized to modify
already-merged prior-task files (`skillpacks/base.py` from T4,
`skillpacks/clinic/pack.py` from T5) — scoped tightly to exactly that,
with `core/interfaces.py` and `core/orchestrator.py` left untouched.
The reviewer independently verified the widening didn't disturb any
existing consumer (grepped `orchestrator`/`gate`/`audit` for `SlotInfo`
usage — only type hints, no construction sites) and hand-checked the
new interval-overlap math against both boundary cases (adjacent, not
overlapping; one-minute overlap).

Both new fields have **no default values** — a deliberate choice so
that every skill pack must supply them explicitly rather than silently
getting the "wrong" default and passing type-checking while producing
nonsensical conflict behavior.

**T10 (`capacity`'s restaurant meaning resolved):** T4's open question —
what does `capacity` mean for a sector with more than one resource unit
per booking — was settled when `RestaurantSkillPack` was built: `capacity`
is the *demand* (`party_size`), not the *supply* (the assigned table's
seat count). This is a documentation-accuracy choice, not a functional
one — `capacity` plays no role in `BookingStore`'s conflict logic at
all (that logic is entirely `resource_key` + interval overlap); it
exists on `SlotInfo` purely as descriptive/audit-facing data. Clinic's
`capacity` is always `1` under the same interpretation (one
practitioner, one patient — not a placeholder, the literal capacity of
a clinic slot).

## 3. `RestaurantSkillPack`'s table-assignment design (T10)

`RestaurantSkillPack.slot_rules()` (`eais_scheduling_agent/skillpacks/restaurant/pack.py`)
assigns a table with a **static, deterministic smallest-fitting-table**
rule: among all configured tables whose capacity is `>= party_size`,
pick the one with the smallest capacity, breaking ties by
lexicographically-smallest table id.

This is *not* a live-availability search, and the reason is structural,
not an oversight: `SchedulingAgentCore.handle()` (T6) calls
`skill_pack.slot_rules(request)` *before* `store.check_conflict(request,
slot)` — see the 9-step flow in `core/orchestrator.py`. By the time
`slot_rules()` runs, nothing has asked the store which tables are free
at the requested time; the skill pack has no interfaces available to it
that would let it ask. Redesigning the interface order to let
`slot_rules()` see store state would mean either (a) passing the store
into every skill pack (breaking the store/pack separation the whole
architecture is built on — a skill pack would gain sector-external
knowledge), or (b) restructuring the core's orchestration flow itself,
which T10's scope explicitly forbids touching (the whole point of T10
was proving a second sector requires zero core changes — see AC5).
Given the existing interfaces, static assignment was the only option
that kept the core untouched, which is what T10 was actually testing.

The accepted trade-off, documented directly in the class's own
docstring (not silently accepted): because assignment reuses T9's exact
same `resource_key` + interval-overlap conflict model, two requests for
the same party size at the same time are correctly assigned the same
table and correctly detected as conflicting. But a request can also be
reported as conflicting with an existing booking on "its" table even
when a *different*, equally-suitable table happens to be free at that
moment — the pack has no way to know that, because table occupancy
lives in `BookingStore`, not in the pack. This is a real, deliberate
simplification, not a bug, and this task doesn't attempt to solve it:
doing so would require giving the skill pack visibility into the
store, which the current interfaces (by design) do not provide.

## 4. The R1-proof test's dynamic design (T11)

`tests/test_r1_proof.py` is the comprehensive, self-maintaining version
of the sector-name check T6 first wrote inline
(`TestCoreHasNoSectorNames` in `tests/test_core_orchestration.py`, which
hardcodes the literal strings `"clinic"`/`"restaurant"`).

Hardcoding has two gaps T11 exists to close:

1. **Not self-maintaining.** A third sector added later wouldn't be
   covered by the check until someone remembered to update a hardcoded
   list by hand — exactly the kind of thing that silently rots.
2. **Only catches literal name strings**, not hidden coupling. Code
   could reference a concrete skill-pack class (directly, or
   transitively through some other module `core/` imports) without ever
   spelling out a sector name as a string literal.

T11's two checks close both gaps by deriving the set of sector names
*at test-run time* rather than hardcoding it:

- **`TestDynamicSectorNameScan`** discovers sector identifiers by
  listing `skillpacks/`'s subpackage directories and reading each
  manifest's `sector:` field via the real `SectorManifest.load` — not a
  hardcoded list — then greps `core/*.py` for any of the discovered
  names.
- **`TestCoreImportGraphNeverLoadsAConcreteSkillPack`** (the stronger,
  primary check) imports each `core/` module in a *fresh subprocess*
  (so nothing is pre-imported from a prior test) and inspects
  `sys.modules` afterwards for any concrete skill-pack subpackage. This
  catches transitive imports too — a module `core/` imports that itself
  pulls in a concrete pack indirectly — which a source-grep alone would
  miss. A bonus static AST check is included alongside it as a faster,
  complementary (not replacement) signal.

Why the dynamism matters for the claim staying true: R1 ("the core has
zero sector-named identifiers") is a claim about the architecture that
is supposed to keep holding as sectors are *added*, not just a
snapshot fact about two sectors that happened to exist when the test
was written. A hardcoded-sector-list version of this test would pass
today and silently stop meaning anything the day a third sector is
added without a matching edit to the test file. Both checks carry
vacuous-pass guards (require `>=2` discovered sectors) so a
degenerate, single-sector state can't produce a false "clean" result
either.

The implementer manually sanity-checked the check's own sensitivity by
injecting the literal text `# clinic` into `core/gate.py`, confirming
the test failed, then reverting — proving the check actually detects
what it claims to detect, not just that it passes by construction. The
reviewer independently re-verified against the live filesystem/API that
both checks are genuinely dynamic (not hardcoded values dressed up to
look dynamic), which was judged the main risk for a test of this kind.

## 5. The LLM intake fallback contract (T13)

`LLMIntake.parse()` (`eais_scheduling_agent/intake/llm.py`) has one
contract that shapes everything else about the module: **it never
raises on account of the LLM.** Every way the LLM path can go wrong —
the Ollama process not running, a connection refusal, a timeout, a
non-2xx response, a response that isn't valid JSON, JSON that parses
but has the wrong shape, individual fields with the wrong type — is
treated as exactly the same signal: "the LLM path didn't work this
time," and `parse()` silently delegates to `self._fallback.parse(text,
sector)` (in practice, `OfflineIntake`, T12) instead.

This is implemented as two independently swappable layers, deliberately
kept apart:

1. **The HTTP client boundary** (`HTTPClient = Callable[[str], str]`).
   `LLMIntake` accepts any zero-state callable taking a prompt and
   returning the model's raw text; the real `OpenAICompatibleHTTPClient`
   (stdlib `urllib.request` only, no new dependency) is the one
   production code always passes in -- `LLMIntake.__init__`'s `client`
   parameter is required, not defaulted, so there is no implicit
   fallback to a concrete client hidden inside `LLMIntake` itself; see
   `intake/llm.py`'s own docstrings. Every exception the client raises
   is caught in one `except Exception` around the single call site and
   mapped to the fallback. Tests inject a fake callable, which is what
   makes "zero real network calls in the test suite" a structural fact
   (dependency injection) rather than a convention someone could
   accidentally violate — confirmed by the reviewer scanning every
   `OllamaHTTPClient` reference in the T13 diff (this class's original
   name at the time, later generalized to `OpenAICompatibleHTTPClient`
   by the configurable-LLM-backend extension) and every test-file
   `LLMIntake` construction.
2. **Response validation** (`_parse_and_validate`), independent of the
   client. Even a *successful* HTTP call's JSON is validated field by
   field against a per-sector schema; a field that fails validation is
   dropped individually (never guessed at, never crashes the whole
   parse) — same "omit, don't guess" contract `OfflineIntake` (T12)
   established. A response that isn't a non-empty JSON object at all is
   treated identically to a network failure (the same fallback path),
   distinct from a response that's a valid object with some invalid
   field values (which returns a smaller, still-valid dict rather than
   `None`).

Why this design point matters beyond "graceful degradation" as a
generality: it makes the system's *availability* independent of
whether a local LLM happens to be running at all. Neither this
development environment nor CI has any LLM runtime installed (see
`HANDOVER.md`'s known gaps) — under a naïve design where `--llm`
failures propagated as errors, the entire `--llm` code path would be
permanently unusable/untestable in exactly the environments this
project is actually developed and verified in. Because `LLMIntake`
never raises, `--llm` is safe to ship, document, and even exercise in
tests (`TestLLMFlagFallsBackCleanly` in `tests/test_cli.py` runs the
real, unmocked `--llm` wiring end to end against no running Ollama
server and asserts it still confirms a valid booking) without ever
touching a real socket.

## 6. The CLI wiring layer (T14)

`eais_scheduling_agent/cli.py` was originally, by design, **the one
place in the project allowed to name a sector** (T14). That knowledge
now lives in `wiring.py`, shared with the optional HTTP interface added
afterward -- `cli.py` remains the CLI-specific wiring (argument parsing,
stdout rendering), but no longer defines the sector-naming mapping
itself. Every prior component (T6–T13) is built and tested against
`core.interfaces` / `skillpacks.base` alone and earns its
sector-agnosticism specifically so that this one layer can stay simple
(see §1).

The other notable design point in this layer is `_CachingIntake`, a
thin wrapper around whichever real `IntakeService` was selected
(`OfflineIntake`, or `LLMIntake` under `--llm`). It exists because of a
specific, deliberate limitation elsewhere: `SchedulingAgentCore.handle()`
returns only a bare `Decision`, never the `BookingRequest` it built
internally — "the core deliberately does not render text" (T6's own
report). But rendering a `CONFIRMED` decision's human-readable
confirmation message needs exactly that request's `fields`
(`skill_pack.confirmation_template().format(**request.fields)`), and
that rendering job belongs to the CLI, not the core.

The naïve fix — call `intake.parse(text, sector)` a second,
independent time after `handle()` returns — was rejected for two
reasons, not one:

- **Cost.** Under `--llm`, a second call means a second real network
  round-trip per request just to re-derive data the core already
  computed once.
- **Correctness.** If the model's sampling isn't perfectly stable, a
  second independent call could in principle return *different* fields
  than the ones the core actually validated, checked for conflicts, and
  persisted — which would make the printed confirmation lie about what
  was actually booked. This is a correctness risk, not merely an
  efficiency one.

`_CachingIntake` memoizes by the exact `(text, sector)` pair: `main()`
calls `intake.parse(args.text, args.sector)` once more after
`handle()` returns `CONFIRMED`, passing the identical arguments
`handle()` already used internally, which is guaranteed to be a cache
hit — so rendering never re-parses and never re-hits an LLM, and the
rendered confirmation is guaranteed to describe the exact fields the
core decided on. The reviewer independently traced this guarantee
against the real code (not just the docstring's claim) before
approving T14.
