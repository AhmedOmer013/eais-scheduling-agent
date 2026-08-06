# Dashboard redesign: pending-approval queue, per-sector audit, clarification messaging — design

**Date:** 2026-08-07
**Status:** Approved by Ahmed, ready for implementation planning.

## Scope note — read this first

Same as every extension before it: **explicitly outside the `EAIS-HR-2159-TA-01` assessment brief's scope**, recorded in `EXTENSIONS.md`, not woven into `RESEARCH.md`/`PLAN.md`/`ARCHITECTURE.md`/`DESIGN.md`. `core/` is untouched by this work — everything here lives in `http_api.py`, a new `pending.py`, and the dashboard templates/static assets.

## Context

Ahmed asked for four things on the web dashboard:
1. A visual redesign (it's currently unstyled/minimal).
2. Split audit trail views for clinic vs. restaurant.
3. A new interface for the human side to accept or reject `PENDING_APPROVAL` requests — a genuinely new capability, since today a `PENDING_APPROVAL` decision is a one-shot HTTP response with no persistence anywhere for later action.
4. A distinct error/clarification message when the input text is too unclear to process.

Brainstormed with the visual companion for layout and style; three product decisions were made explicit before designing:

1. **"Missing fields" and "violation/conflict" are different things.** Missing fields (intake couldn't extract enough) means there's no complete booking to review — it becomes an inline "needs clarification" message, not a queue item. Violations and conflicts (unknown practitioner, over capacity, double-booking) mean the booking *is* fully understood, just needs a human's judgment call — these are what enters the new pending queue.
2. **Accept = a real confirmed booking.** Clicking Accept persists the slot to the shared store (so it counts for future conflict checks) and appends a new `CONFIRMED` audit record. Reject discards it with an audit record; no slot persisted.
3. **The pending queue survives a server restart** (file-backed), unlike the existing in-memory booking store.

## Visual design

**Navigation**: top tab bar — **Book | Pending (N) | Audit: Clinic | Audit: Restaurant | Config**. Replaces the current single scrolling page. The Pending tab shows a live count badge.

**Style**: "Warm Neutral" — cream background, terracotta accent, rounded corners, softer typography than a stock corporate dashboard. Approved from mockups. Concrete palette:

| Token | Value | Use |
|---|---|---|
| Background | `#fdf8f3` | Page background |
| Card background | `#fffefb` | Cards, form panels |
| Border | `#ecd9c4` | Card/input borders |
| Accent (terracotta) | `#c2703d` | Active tab, primary actions, Reject button, Pending-tab badge |
| Confirmed (green) | `#3f8a5c` | Accept button, Confirmed message |
| Muted text | `#8a7863` | Secondary text, labels |
| Body text | `#3f342a` | Primary text |
| Clarification (muted rose) | `#b5786a` on `#f6ece7` tint | Needs-clarification message — distinct from both the accent (actionable/terracotta) and confirmed (green), signaling "informational, not actionable" |
| Font | `Georgia` (headings) / `Segoe UI` (body/UI) | Matches the approved mockup's warmer, less corporate feel |
| Radius | `10–12px` on cards/buttons | Consistent rounded-corner language throughout |

**Pending Requests tab**: card list, inline Accept/Reject buttons directly on each card (no separate detail-panel step — approved over that alternative since a pending item's text + reason is already everything relevant to the decision).

## Components

### 1. `eais_scheduling_agent/pending.py` (new)

```python
class PendingRequestStore:
    """File-backed queue of violation/conflict PENDING_APPROVAL requests
    awaiting a human accept/reject decision. Survives server restarts
    (unlike InMemoryBookingStore) -- a JSON file (default
    pending_requests.json, gitignored), a dict keyed by request id,
    rewritten in full on every mutation. Prototype-scale data (a human
    review queue, not a high-volume log), so whole-file rewrite is
    simple and sufficient -- no partial-write/append format needed.
    """
    def add(self, sector: str, text: str, fields: dict, skill_pack: str, reason: str) -> str:
        """Persist a new pending item, return its id (uuid4 hex)."""

    def list(self, sector: Optional[str] = None) -> list[dict]:
        """All pending items, optionally filtered by sector."""

    def get(self, request_id: str) -> Optional[dict]:
        ...

    def remove(self, request_id: str) -> None:
        """Called after accept/reject resolves an item."""
```

Missing/corrupt file on startup → treated as empty, not a crash (same tolerance pattern the audit trail already has for its own I/O).

### 2. `http_api.py` extensions

`create_app()` additionally builds:
- `audit_by_sector: Dict[str, JsonLinesAuditTrail]` — `{"clinic": ..., "restaurant": ...}`, replacing the single shared `audit`. `post_booking()` picks `audit_by_sector[sector]` when constructing its per-request `SchedulingAgentCore` (already built fresh per request today — this is a lookup, not new plumbing).
- `pending_store: PendingRequestStore`.

**`POST /bookings`** — decision logic unchanged. New: on `PENDING_APPROVAL`, classify via `decision.reason.startswith("missing required field(s):")`:
- **True** → "needs clarification," not queued. Response becomes `{"status": "NEEDS_CLARIFICATION", "reason": "..."}` (new status string, HTTP layer only — `core.models.Decision` still only ever produces `CONFIRMED`/`PENDING_APPROVAL`, this is a presentation-layer relabeling).
- **False** → `pending_store.add(...)`, response stays `{"status": "PENDING_APPROVAL", "reason": "..."}`.

**`GET /pending?sector=`** — list pending items, optionally filtered.

**`POST /pending/<id>/accept`**:
1. Look up the item; `404` if missing.
2. Re-derive `SlotInfo` via the sector's skill pack (same method `core/orchestrator.py` already calls internally) from the stored `fields`.
3. Re-check conflict against the *current* shared store state — `409` (no side effects, item stays pending) if something else has since taken the slot.
4. Persist the slot to the shared store; append a new audit record (`decision="CONFIRMED"`, `approval_status="approved"`) to `audit_by_sector[sector]`.
5. Remove from `pending_store`.

**`POST /pending/<id>/reject`**:
1. Look up the item; `404` if missing.
2. Append a new audit record (`decision="PENDING_APPROVAL"`, `approval_status="rejected"`) to `audit_by_sector[sector]`. No slot persisted.
3. Remove from `pending_store`.

**`GET /audit?sector=clinic|restaurant`** — reads `audit_by_sector[sector]`'s file. `sector` is optional: omitting it preserves today's behavior — a merged view of both files, sorted by timestamp — so the existing `GET /audit` tests and callers keep working unchanged. The two-tab UI always passes `sector` explicitly.

Scope boundary: this split audit-by-sector wiring is **web-server only**. `cli.py` keeps its existing single `audit.jsonl` (still overridable via `--audit-file`), unaffected — this was specifically a UI ask.

### 3. Dashboard (`templates/dashboard.html`, `static/style.css`, `static/app.js`)

Rebuilt around the tab bar and palette above. Booking-result messages get three visually distinct states (Confirmed/green, Needs clarification/rose, Pending review/terracotta) instead of today's undifferentiated pending-vs-confirmed text. Clarification wording: e.g. *"We couldn't quite process that — patient_name is missing. Try rephrasing with more detail."* (names the specific missing field(s), friendlier framing than today's raw `"Pending approval: missing required field(s): ..."`).

## Error handling

- `POST /pending/<id>/accept|reject` on an unknown/already-resolved id → `404`.
- `POST /pending/<id>/accept` on a since-conflicted slot → `409`, nothing persisted, item stays pending.
- `PendingRequestStore` file missing/corrupt on startup → empty, not a crash.
- Same known limitation as the existing shared `store`: the dev server is threaded, no locking — two truly concurrent accepts of the same item aren't guaranteed to serialize correctly. Documented, not fixed (matches this prototype's existing risk posture for the booking store).

## Explicitly out of scope

- Authentication (still none, consistent with every other endpoint here).
- Any live notification back to whoever originally submitted a now-resolved pending request — no session/identity system exists or is planned; accept/reject is a record-keeping and store-consistency action, confirmed explicitly with Ahmed.
- Playwright e2e coverage of this new workflow (extension #3 is still not started; whenever it is built, it should exercise this).

## Testing

Same pattern as `tests/test_http_api.py` (Flask `test_client`, no real sockets):
- `PendingRequestStore`: add/list/get/remove, file round-trip, missing/corrupt-file tolerance.
- `POST /pending/<id>/accept`: happy path (becomes CONFIRMED, persisted, removed from queue, audit updated), 404, the 409 re-conflict case.
- `POST /pending/<id>/reject`: happy path, 404.
- `GET /pending` and `GET /audit`, both with and without `?sector=`.
- The missing-fields-vs-violation classification (`NEEDS_CLARIFICATION` vs `PENDING_APPROVAL` response status).
