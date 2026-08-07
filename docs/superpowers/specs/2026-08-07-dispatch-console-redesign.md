# Dispatch Console redesign (2026-08-07)

## Why

The dashboard's Warm Neutral look (cream ground `#fdf8f3`, Georgia serif
headings, terracotta accent -- see `EXTENSIONS.md`'s 2026-08-07 tab-bar
redesign entry) is a textbook instance of the pattern AI-generated UIs
converge on regardless of subject: warm cream ground, serif display,
terracotta/signal-red accent. The repo owner asked for "a new polished
design" that keeps every existing capability intact. This is a redesign,
not a refinement: the old look is treated as evidence of what the
surface is (an operator console for a booking core), not as a brand
worth preserving.

## Surface brief

Mode: **Operate**. The visitor is the repo owner running this locally to
make bookings, clear the pending queue, read the audit trail, and edit
slot rules -- not a customer-facing surface. Per `PRODUCT.md`, every
visual element must map to a real capability, status must never be
misreadable, and scanability outranks expressive flourish.

## Direction: "Dispatch Console"

A dispatcher's/ops-console register, not a generic SaaS dashboard and not
the warm-editorial default: a dark instrument-panel header and pill tab
bar sit above a light paper canvas; white "ticket" surfaces hold each
form, card, and table. Status (confirmed / pending / needs-clarification
/ error) is color-coded consistently everywhere it appears -- result
flashes, the pending queue's reason chips, badge counts -- via one
dot+chip visual language, never via a colored border (which the design
system's craft floor treats as decoration above 1px).

Typography is a deliberate two-voice system: **IBM Plex Sans** for UI
chrome (labels, headings, buttons) and **IBM Plex Mono** for anything
that is data -- timestamps, decisions, durations, table values -- so the
mono use is functional (marking "this is measured/read data"), not a
costume for "technical." Accent color is indigo (`#3454d1`), replacing
terracotta; the Restrained strategy (neutrals plus one accent) fits an
Operate surface, applied at a level of specificity -- the dispatch
register metaphor, the header/canvas split, the two-voice type system --
that a generic "clean dashboard" prompt would not produce on its own.

## Scope note: reduced ceremony

Impeccable's `new-work` flow calls for a dice-rolled, multi-candidate
visual-direction board (`concept-seed.mjs` + `serve-question.mjs`) before
committing to a world. That step was skipped here: the repo owner asked
to see the redesign locally "now," this is a solo local tool with no
brand stakes riding on the choice, and PRODUCT.md was itself written by
inference from existing docs (`EXTENSIONS.md`, `README.md`,
`wiring.py`) rather than a live interview, disclosed to the repo owner
at the time. A single, fully-committed direction was chosen and built
directly instead. This is a scope reduction, not a skipped review: the
build still went through the skill's craft-floor checklist (contrast,
spacing, states, anti-pattern list) and the mechanical detector
(`detect.mjs`), which came back clean after one round (an initial
"overused font" flag on Inter was fixed by switching to IBM Plex Sans).

## What changed, functionally

Nothing. Every id, class, form field name, and event binding
`static/app.js` depends on is unchanged (see `PRODUCT.md`'s Capabilities
and Constraints section for the full list). The one JS edit was to
`withLoading()`, which used to swap a loading button's entire
`textContent`; it now swaps a `.btn-label` child span when the button
has one, so the new inline icons on action buttons survive a loading
cycle without changing disable/restore timing or behavior for any
button. `static/style.css` and `templates/dashboard.html` carry the
actual redesign.
