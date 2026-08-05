# PLAN.md — Implementation Plan

Committed before any feature code, per the brief's plan-first rule. Requirements referenced below (`FRx`, `NFR-*`, `ACx`) are the ones defined in `docs/document-pack/SRS.docx` (D1) — that document, not the raw brief, is the authoritative requirements source this plan is built against. See `RESEARCH.md` for the technology survey and `ARCHITECTURE.md` for the component/sequence/data-contract detail this plan builds toward.

Before starting T1, read [`diagrams/DIAGRAMS.md`](diagrams/DIAGRAMS.md) (also exported as `diagrams/UML_Diagrams.docx`) — it walks activity → sequence → class and the class diagram there is what T2-T13 are implemented against directly.

## 1. Task breakdown

Each task is independently verifiable and, where it touches behavior, ships with its own tests rather than deferring tests to the end. The `Satisfies` column traces each task back to the SRS requirement it exists to fulfil.

| # | Task | Done when | Satisfies |
|---|---|---|---|
| T1 | Repo scaffold, `.gitignore`, empty package layout | `pip install -e .` (or equivalent) runs clean | NFR-Installability |
| T2 | Core data model: `BookingRequest`, `Decision`, `AuditRecord` types | Types defined, importable, unit-testable in isolation | FR6, FR7 |
| T3 | Sector manifest loader (YAML/JSON → validated manifest object) | Loader rejects a malformed manifest with a clear error; test included | FR3 |
| T4 | Skill-pack interface (required fields, validation rules, working hours, slot rules, confirmation template) | A minimal fake skill pack satisfies the interface in a test | FR2 |
| T5 | Clinic skill pack (concrete implementation) | Fixed-slot-length, named-practitioner booking validated by its own tests | FR2 |
| T6 | Scheduling Agent Core — orchestration only (calls manifest + skill pack, no sector knowledge) | Core module contains no sector-named identifier (grep-checkable) | FR1 |
| T7 | Approval/permission gate | Out-of-hours, double-booking, over-capacity, missing-field cases all return `PENDING_APPROVAL` + reason | FR5 |
| T8 | Audit trail (JSON lines) | Every decision path (confirmed, pending, rejected) produces one audit record; test asserts schema | FR6, NFR-Auditability |
| T9 | Persistence + same-run conflict detection (in-memory or SQLite) | Two overlapping bookings in one run correctly conflict; boundary-case tests (adjacent, not overlapping) | FR7 |
| T10 | Restaurant skill pack (second sector) | Capacity/party-size, flexible-duration booking validated by its own tests; added by writing a manifest + skill pack only, zero core diffs | FR1, FR2, AC5 |
| T11 | R1-proof test | A test that fails if sector-specific logic is reintroduced into the core | FR1, AC1 |
| T12 | Deterministic offline intake (rule/regex-based text → structured request) | Runs with no network; identical input → identical output across runs | FR4, AC2, AC7 |
| T13 | LLM-backed intake (local model, runtime TBD) | Same interface as T12, swapped via config; falls back to offline mode when unavailable | FR4 |
| T14 | CLI and/or HTTP interface | One documented command runs an end-to-end booking from free text | NFR-Installability |
| T15 | README, DESIGN, AI_USAGE, HANDOVER docs | Each exists and matches what's actually true of the code at submission time | AC6 |

## 2. Build order and rationale

1. **T1-T4** (scaffold → types → manifest → interface) — nothing sector-specific exists yet; this is where the FR1 contract is *designed*, before it can be violated.
2. **T5-T9** (clinic sector + core + gate + audit + persistence) — build the core against exactly one sector first. Proving the abstraction against only one sector would be too easy to fake; proving it needs a second sector, which is why T10 comes next rather than being folded in early.
3. **T10-T11** (restaurant sector + R1-proof test) — the moment of truth against AC5: if T10 requires touching the core, the design is wrong and needs revisiting before continuing. Deliberately sequenced as its own checkpoint rather than built in parallel with T5-T9.
4. **T12-T13** (offline intake, then LLM intake) — offline first because it's the mode AC2/AC7 are actually graded against, and because the LLM path can be developed and swapped in behind the same interface once it exists.
5. **T14** (interface) — thin layer on top of everything else; deliberately last since it has no bearing on FR1-FR8.
6. **T15** (docs) — written alongside the work as it happens (README/DESIGN evolve continuously), finalized once implementation is stable, checked against AC6.

## 3. Time budget

Against a ~10-12 hour total, ~3 hours already spent on Stage A (this document + `RESEARCH.md` + `ARCHITECTURE.md` + `SRS.docx`), leaving roughly 7-9 hours for Stage B:

| Tasks | Budget |
|---|---|
| T1-T4 (scaffold, types, manifest, interface) | 1.0 hr |
| T5-T9 (clinic + core + gate + audit + persistence) | 3.0 hrs |
| T10-T11 (restaurant + R1-proof test) | 1.5 hrs |
| T12-T13 (offline + LLM intake) | 1.5 hrs |
| T14 (CLI/HTTP) | 0.5 hr |
| T15 (docs, polish, README verification) | 1.0 hr |

## 4. Top risks

1. **Non-deterministic LLM step vs. the deterministic-offline requirement (FR4, AC2, AC7).** Contained by designing the offline mode as the primary, always-tested path from T12 onward, and treating the LLM path as a swappable adapter behind the same interface rather than the thing tests depend on.
2. **FR1 silently drifting** (a "just this once" sector check creeping into the core under time pressure, which would also fail AC1 and AC5). Contained by writing the R1-proof test (T11) early relative to the rest of Stage B, not as an afterthought, and treating T10 as a hard checkpoint rather than a formality.
3. **Slot/boundary logic diverging** between a fixed-length-slot model (clinic) and a flexible-duration/capacity model (restaurant), which would undermine FR7 and NFR-Extensibility. Contained by modeling both as "a request for capacity over an interval" in the shared core, with only the interval-and-capacity *rules* coming from the skill pack — tested explicitly at adjacent, overlapping, and boundary-equal cases (T9).

## 5. Definition of done

Directly the SRS acceptance criteria, plus the process requirements from the brief:

- **AC1** — core module has zero sector-named identifiers; R1-proof test passes.
- **AC2** — documented test command exits 0 with no network access.
- **AC3** — both sectors: an out-of-hours/over-capacity/double-booked/missing-field request returns `PENDING_APPROVAL` with a specific reason.
- **AC4** — every processed request yields exactly one matching audit record.
- **AC5** — per commit history, adding the restaurant sector touched only manifest/skill-pack/test files.
- **AC6** — README alone → running system + passing tests, clean machine, under 5 minutes.
- **AC7** — offline intake output is identical for identical input across repeated runs.
- Git history shows Stage A docs committed before any implementation commit, with incremental, meaningfully-messaged commits after (no squashed "initial commit").
- All required docs (`RESEARCH.md`, `PLAN.md`, `ARCHITECTURE.md`, `README.md`, `DESIGN.md`, `AI_USAGE.md`, `HANDOVER.md`, `SRS.docx`) exist and are accurate as of the submission commit.
- `diagrams/class-diagram.mmd` still matches the actual class names/relationships in the code — if a class gets renamed or restructured during T2-T13, the diagram (and its `.docx` export) gets updated in the same commit, not left stale.
- Any gap that remains is documented explicitly (what's missing, why, how it'd be finished) rather than left silent.

## 6. What gets cut first if time runs short

In order: (1) HTTP interface — keep CLI only, since neither is required by any AC; (2) screen recording and task board — already optional; (3) breadth of offline-intake phrasing variety — keep the core cases needed for AC7, drop edge-case phrasing; (4) restaurant skill pack polish beyond what AC5 requires. **Never cut:** the approval gate (AC3), the audit trail (AC4), the R1-proof test (AC1), or documenting a gap honestly instead of hiding it.

## 7. Plan vs. actual

The build order held exactly as planned — T1 through T15, in sequence, each merged via its own reviewed PR, with no task started out of order and no sector-specific shortcut taken to get T10 done faster. The T5-T9 checkpoint worked as intended: by the time T10 (the restaurant sector) landed, it touched only a new manifest and a new skill-pack package, zero diffs to `core/`, matching the AC5 claim on the first attempt rather than after cleanup.

Wall-clock time came in well under the ~10-12 hour estimate — Stage A through T15 closed in a single continuous session rather than spread across days — but the effort *shape* matched the budget's relative proportions closely: T5-T9 was the largest single block, T14 the smallest, exactly as allocated.

The design changed on contact with the code once, concretely: `SlotInfo` needed widening with `resource_key` and `start` fields (see the "T9 prep" commit) that `ARCHITECTURE.md`'s original data contract hadn't anticipated — conflict detection needed to know *what* resource and *when*, not just a duration, to compare two bookings. This was caught before T9 itself was implemented, not after, so it cost a small prep commit rather than a rework.

Two real bugs were caught by review rather than by original test-writing: a `JsonLinesAuditTrail` crash serializing a `datetime` nested inside a list (fixed, with a regression test added specifically for that recursion branch), and a diagram/implementation drift after T6 that required a dedicated sync pass. Neither reached `develop` unfixed.

Nothing on the plan's cut list (§6) was needed — no AC was dropped or left an undocumented gap. The one genuine documentation debt still open past T15 is cosmetic: `diagrams/UML_Diagrams.docx` not yet regenerated to match the post-T15 class diagram, tracked honestly in `HANDOVER.md` rather than silently left stale.
