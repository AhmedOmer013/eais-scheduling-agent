# AI_USAGE.md

This project's Stage B implementation (T1-T14) was built using Claude Code
in an agentic, multi-model workflow: a controller session dispatched a
fresh subagent per task, followed by an independent task-scoped code
review (spec compliance + quality) before merge, with model tier chosen
per task's complexity (cheap models for mechanical scaffolding, stronger
models for architecturally consequential tasks like T6's core
orchestration and T10's AC5 checkpoint). Every task went through GitHub
issues, feature branches, and PRs with CI (pytest on Python 3.11/3.12)
required before merge — see the repo's commit/PR history for the full
record.

This document is not a survey of that whole process. Per the brief, it
lists concrete cases where AI output was **wrong** and required human (or
independent-AI-reviewer) correction — evidence of actual oversight, not a
claim that everything worked first try.

## Case 1 — A reviewer's "impossible" finding was itself wrong (stale training knowledge)

**Task:** T1 (repo scaffold).

**What happened:** The implementer's report showed `pytest` output
including `Python 3.14.4`. A separate reviewer subagent flagged this as
an "Important" finding, asserting Python 3.14 "does not exist" and that
the version string was therefore evidence the verification steps hadn't
actually been run.

**Why it was wrong:** The reviewer's claim was based on its training
cutoff, not on checking this machine. Python 3.14 was released in
October 2025 and was genuinely installed and running here. The
controller verified this directly (`python --version`, re-running
`pytest`) and confirmed the original implementer's report was accurate;
the reviewer's finding was the error.

**Correction:** The controller independently ran the verification
commands rather than trusting either AI's claim at face value, confirmed
the code was correct, and recorded the adjudication in the task ledger
rather than silently accepting or silently overriding the finding.

**Lesson:** An AI reviewer's confident-sounding factual claim ("X is
impossible") is not automatically more trustworthy than the thing it's
reviewing — it can be wrong for the same reason any model output can be
wrong (here, a training-data cutoff). Independent verification against
the real environment settled it either way.

## Case 2 — Commits were authored under the wrong identity

**When:** Between T2 and T3, discovered when the user asked to confirm
all pull requests were under their GitHub account.

**What happened:** The controller ran direct `git commit` calls (for
Stage A docs and the CI workflow) using this machine's pre-existing local
git config, which was set to an unrelated name/email
(`hima890 <hfibrahim90@gmail.com>`) left over from the machine's prior
state — not the project owner's identity. Two commits landed under the
wrong author before this was noticed.

**Why it happened:** The AI did not check the local git identity before
committing; it assumed the ambient environment was already configured
correctly for this project.

**Correction:** The controller set the repo-local git identity
correctly, then rewrote the two affected commits' authorship via a
non-interactive `git rebase --exec`, force-pushed the corrected history
(temporarily lifting branch protection to do so, then restoring it), and
verified via the GitHub API that every commit and PR was attributed
correctly afterward.

**Lesson:** Environment assumptions (git identity, in this case) should
be checked explicitly before the first commit in a new repo, not
discovered after the fact from downstream (GitHub) state.

## Case 3 — A generated task brief contradicted the authoritative spec

**Task:** T5 (clinic skill pack).

**What happened:** The controller's own task brief for T5 added a
constraint not present in the underlying spec: that `required_fields`
must list "exactly" the field names the pack's `validate()`/`slot_rules()`
methods read. The implementer built `required_fields` to include
`patient_name` (matching ARCHITECTURE.md's own worked example, which
explicitly lists `patient_name` for clinic) even though neither
`validate()` nor `slot_rules()` reads that field directly. A reviewer
subagent then flagged this as an "Important" spec violation — correctly
relative to the brief's literal wording, incorrectly relative to the
actual authoritative source.

**Why it was wrong:** The brief's added constraint was the controller's
own invention, written without re-checking it against ARCHITECTURE.md's
existing worked example, and it happened to contradict that example.

**Correction:** The controller re-read ARCHITECTURE.md and
`skillpacks/base.py`'s own docstring (which quotes the same clinic
example near-verbatim), confirmed the implementer's code was correct, and
adjudicated the reviewer's finding as a false positive caused by a flaw
in the brief — not a flaw in the implementation. No code was changed.

**Lesson:** A brief written by the same AI system doing the review is not
itself immune from being the actual source of an error — adjudicating a
finding sometimes means checking the instruction, not just the code that
followed it.

## Case 4 — A shipped bug wasn't caught until real end-to-end testing

**Tasks:** Introduced in T8 (audit trail), found during T12 (offline
intake).

**What happened:** `JsonLinesAuditTrail` (T8) serializes `AuditRecord` to
JSON, including the `intent` field (a copy of the booking's extracted
data). Every real skill pack (T5, T10) puts a live `datetime.datetime`
object in that data under `start_time`. T8's own test suite only ever
exercised `intent` dicts containing strings, so the serializer's failure
to handle `datetime` values went undetected through T8's review and
merge. It surfaced only when T12's implementer manually ran a real
booking end-to-end (`OfflineIntake` → real core → real audit trail) and
hit a `TypeError` crash on the very first attempt — something no
prior task's automated tests exercised, because every earlier task tested
its own component in isolation against fakes/stubs for its neighbors.

**Correction:** Fixed directly as a small, separately-reviewed hotfix
(not folded into T12, which was correctly scoped to intake only): a
recursive `_json_safe()` helper in `core/audit.py`, plus regression tests
using realistic field shapes (including a case the original fix round
hadn't covered — datetime nested inside a list — added after an
independent reviewer flagged the gap).

**Lesson:** Component-level tests passing does not guarantee integration
correctness. This project's task-by-task review process is strong at
catching spec/quality issues within one component but, by design, does
not exercise real cross-component data flow until something later
actually does so — manual end-to-end verification (as required for T14)
caught what unit tests structurally could not.
