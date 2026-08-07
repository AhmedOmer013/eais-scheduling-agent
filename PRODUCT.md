# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
[Inferred from EXTENSIONS.md/README.md, not confirmed by interview.] The repo owner, running the dashboard locally as a solo operator/developer to demo and exercise the EAIS scheduling agent -- making bookings, reviewing items that need a human call, editing per-sector slot rules, and switching the LLM backend. No multi-user or role separation exists; there is no authentication.

## Product Purpose
A local, single-page operational console for the EAIS multi-sector scheduling agent (clinic + restaurant). It turns free-text booking requests into confirmed/pending/needs-clarification decisions, queues ambiguous or rule-violating requests for a human accept/reject call, keeps a per-sector audit trail, and lets the operator edit each sector's slot rules (practitioners/tables, working hours) and the runtime LLM config -- all without a restart.

## Positioning
[Inferred.] Not a product surface for end customers -- a working instrument for observing and steering a sector-agnostic booking core, where every panel maps 1:1 to a real backend capability (POST /bookings, /pending accept-reject replaying the core's own decision logic, per-sector audit files, live-editable slot rules). Its credibility comes from that fidelity, not from visual polish for its own sake.

## Operating Context
Run locally via `run.ps1`/`run.sh` or the Flask dev server, opened in a desktop browser at localhost. Five panels/tabs: Book, Pending, Audit: Clinic, Audit: Restaurant, Config. Real-time-ish: pending count badge, inline result flashes, tables refreshed on demand. No mobile usage scenario is evidenced, but the surface should not break on a narrow viewport.

## Capabilities and Constraints
- Functional elements are fixed by `eais_scheduling_agent/static/app.js`: tab switching (`.tab-button`/`.tab-panel`/`data-tab`), booking form (`#booking-form`, `#sector`, `#text`, `#use-llm`), result flashes (`.result` + `status-confirmed`/`status-pending`/`status-clarify`/`status-error`), pending queue (`#pending-list`, `#pending-badge`, `#refresh-pending`, dynamically rendered `.pending-card` with `.meta`/`.text`/`.reason`/`.actions .accept/.reject`), audit tables (`#audit-body-clinic`/`#audit-body-restaurant`, `.refresh-audit[data-sector]`), slot rules (`#clinic-rules-display`/`#restaurant-rules-display`, dynamically rendered `.rules-row` with `.label`/`.value`/`.delete-item`, `.toggle-edit[data-target]`, `#clinic-rules-form`/`#restaurant-rules-form` and their named field ids, `.hidden` class), and config (`#config-form` and its named field ids, `#api-key-hint`).
- A visual redesign must preserve every id/class above exactly (JS queries them directly) and every existing user-facing behavior; only markup structure around them and all CSS are open.
- No build step, no JS framework -- plain HTML/CSS/JS served by Flask (`templates/dashboard.html`, `static/style.css`, `static/app.js`).
- Out of scope for the underlying `EAIS-HR-2159-TA-01` brief entirely (see `EXTENSIONS.md`) -- this dashboard is a personal extension the repo owner asked for after being told it goes against the brief's own scope guidance. It is never submitted as part of the assessment.

## Evidence on Hand
No product screenshots, brand assets, or customer evidence exist. The only "evidence" is the current implementation itself (`static/style.css`'s Warm Neutral look, documented in `EXTENSIONS.md`'s 2026-08-07 redesign note), which this redesign treats as the incumbent look to replace, not a brand to preserve.

## Product Principles
1. Every visual element must map to a real, working capability -- no decorative chrome that implies functionality the backend doesn't have.
2. Status (confirmed / pending / needs-clarification / error) is the one piece of information the operator must never misread at a glance; color-code it consistently everywhere it appears.
3. This is an Operate surface: scanability, information density, and native form/table affordances outrank expressive flourish.
4. Solo local tool, not a multi-tenant SaaS -- no onboarding, empty-account marketing, or growth-oriented UI patterns belong here.

## Accessibility & Inclusion
[Inferred, not confirmed.] No stated requirement beyond ordinary web accessibility (contrast, keyboard operability, focus visibility) -- worth honoring since form-heavy operator tools are used repeatedly and precisely.
