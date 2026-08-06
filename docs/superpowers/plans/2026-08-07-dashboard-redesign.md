# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the web dashboard around a tab-based Warm Neutral UI, split the audit trail into genuinely separate per-sector files, add a file-backed human accept/reject queue for `PENDING_APPROVAL` requests, and distinguish "needs clarification" (missing fields) from "needs human review" (violations/conflicts).

**Architecture:** All new code lives in `http_api.py`, a new `pending.py` module, and the dashboard templates/static assets — `core/` is untouched. The existing "build a fresh `SchedulingAgentCore` per request" pattern in `post_booking()` is extended with a sector-keyed audit dict instead of one shared trail. A new `PendingRequestStore` (file-backed JSON) holds violation/conflict requests between submission and a human's accept/reject decision; accepting one replays the exact `slot_rules()` → `check_conflict()` → `persist()` → `audit.append()` sequence `core/orchestrator.py` already uses internally, so the pending queue never duplicates or drifts from the core's own decision logic.

**Tech Stack:** Python 3.10+, Flask (`http` extra), stdlib `json`/`pathlib` (no new dependencies), vanilla HTML/CSS/JS (no build step, matching the existing dashboard).

## Global Constraints

- Out of scope for the `EAIS-HR-2159-TA-01` assessment brief — see `EXTENSIONS.md`. `core/`, `RESEARCH.md`, `PLAN.md`, `ARCHITECTURE.md`, `DESIGN.md` are not touched by this plan.
- No authentication anywhere (matches every existing endpoint).
- No live notification back to whoever originally submitted a now-resolved pending request (confirmed with Ahmed — record-keeping only).
- Backward compatibility: `create_app(audit_file=...)` keeps working with a single path argument; `GET /audit` with no `sector` param keeps returning a merged, chronologically-sorted list (existing test `tests/test_http_api.py::TestAuditEndpoint::test_returns_one_record_per_request` must keep passing unchanged).
- Visual palette (exact values, from the approved spec):
  - Background `#fdf8f3`, card background `#fffefb`, border `#ecd9c4`
  - Accent (terracotta) `#c2703d` — active tab, primary actions, Reject button, Pending-tab badge
  - Confirmed (green) `#3f8a5c` — Accept button, Confirmed message
  - Muted text `#8a7863`, body text `#3f342a`
  - Clarification (muted rose) `#b5786a` text on `#f6ece7` background tint
  - Font: `Georgia, serif` for headings, `"Segoe UI", sans-serif` for body/UI
  - Border radius `10px`–`12px` on cards/buttons throughout

---

## File Structure

**New:**
- `eais_scheduling_agent/pending.py` — `PendingRequestStore`
- `tests/test_pending.py` — its tests

**Modified:**
- `eais_scheduling_agent/http_api.py` — sector-keyed audit, `PendingRequestStore` wiring, `NEEDS_CLARIFICATION` classification, `GET/POST /pending`, `GET /audit?sector=`, `GET/POST /config/clinic`, `GET/POST /config/restaurant`
- `tests/test_http_api.py` — new test classes appended (existing tests untouched)
- `eais_scheduling_agent/skillpacks/clinic/pack.py` — add a read-only `practitioners` property
- `eais_scheduling_agent/skillpacks/restaurant/pack.py` — add a read-only `tables` property
- `tests/test_clinic_skillpack.py` / `tests/test_restaurant_skillpack.py` — new tests appended
- `eais_scheduling_agent/templates/dashboard.html` — full rewrite (tab bar, slot-rules sections)
- `eais_scheduling_agent/static/style.css` — full rewrite (Warm Neutral palette, slot-rules styling)
- `eais_scheduling_agent/static/app.js` — full rewrite (tab switching, pending queue, split audit, config, slot-rules read/edit)
- `.gitignore` — add the two derived per-sector audit filenames
- `EXTENSIONS.md` — document this as an update to extension #2
- `README.md` — update the "Web dashboard" section

---

### Task 1: `PendingRequestStore`

**Files:**
- Create: `eais_scheduling_agent/pending.py`
- Test: `tests/test_pending.py`

**Interfaces:**
- Produces: `PendingRequestStore(path: Union[str, Path])`, with methods:
  - `add(self, sector: str, text: str, fields: dict, skill_pack: str, reason: str) -> str` — returns a new `uuid4().hex` id
  - `list(self, sector: Optional[str] = None) -> List[dict]` — each dict has keys `id, sector, text, fields, skill_pack, reason, created_at` (fields' `start_time`, if present, is a real `datetime`)
  - `get(self, request_id: str) -> Optional[dict]` — same shape as one `list()` item, or `None`
  - `remove(self, request_id: str) -> None` — no-op if the id doesn't exist

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for PendingRequestStore (file-backed human-review queue)."""

from datetime import datetime

import pytest

from eais_scheduling_agent.pending import PendingRequestStore


@pytest.fixture
def store(tmp_path):
    return PendingRequestStore(path=tmp_path / "pending_requests.json")


class TestAddAndGet:
    def test_add_returns_an_id_and_get_returns_the_item(self, store):
        request_id = store.add(
            sector="clinic",
            text="Dr. Chen today at 10am, patient John Doe",
            fields={"practitioner": "Dr. Chen", "patient_name": "John Doe"},
            skill_pack="clinic_v1",
            reason="unknown practitioner: 'Dr. Chen'",
        )

        item = store.get(request_id)

        assert item["id"] == request_id
        assert item["sector"] == "clinic"
        assert item["text"] == "Dr. Chen today at 10am, patient John Doe"
        assert item["fields"] == {"practitioner": "Dr. Chen", "patient_name": "John Doe"}
        assert item["skill_pack"] == "clinic_v1"
        assert item["reason"] == "unknown practitioner: 'Dr. Chen'"
        assert "created_at" in item

    def test_get_unknown_id_returns_none(self, store):
        assert store.get("does-not-exist") is None

    def test_add_generates_distinct_ids(self, store):
        first = store.add(
            sector="clinic", text="a", fields={}, skill_pack="clinic_v1", reason="r"
        )
        second = store.add(
            sector="clinic", text="b", fields={}, skill_pack="clinic_v1", reason="r"
        )
        assert first != second


class TestStartTimeRoundTrips:
    """fields['start_time'], when present, is a real datetime -- the one
    field every skill pack's slot_rules() needs as a real datetime object,
    not a string (see intake/llm.py's module docstring on this same
    convention). The store must serialize it for the JSON file and
    deserialize it back to a datetime on read, same reasoning as
    core/audit.py's _json_safe.
    """

    def test_start_time_survives_a_round_trip_as_a_real_datetime(self, store):
        request_id = store.add(
            sector="clinic",
            text="Dr. A tomorrow at 9am, patient Jane Roe",
            fields={
                "practitioner": "Dr. A",
                "patient_name": "Jane Roe",
                "start_time": datetime(2026, 8, 10, 9, 0, 0),
            },
            skill_pack="clinic_v1",
            reason="requested slot conflicts with an existing booking",
        )

        item = store.get(request_id)

        assert item["fields"]["start_time"] == datetime(2026, 8, 10, 9, 0, 0)
        assert isinstance(item["fields"]["start_time"], datetime)


class TestList:
    def test_list_with_no_sector_returns_everything(self, store):
        store.add(sector="clinic", text="a", fields={}, skill_pack="clinic_v1", reason="r")
        store.add(
            sector="restaurant", text="b", fields={}, skill_pack="restaurant_v1", reason="r"
        )

        assert len(store.list()) == 2

    def test_list_filtered_by_sector(self, store):
        store.add(sector="clinic", text="a", fields={}, skill_pack="clinic_v1", reason="r")
        store.add(
            sector="restaurant", text="b", fields={}, skill_pack="restaurant_v1", reason="r"
        )

        clinic_items = store.list(sector="clinic")

        assert len(clinic_items) == 1
        assert clinic_items[0]["sector"] == "clinic"

    def test_list_on_empty_store_returns_empty_list(self, store):
        assert store.list() == []


class TestRemove:
    def test_remove_deletes_the_item(self, store):
        request_id = store.add(
            sector="clinic", text="a", fields={}, skill_pack="clinic_v1", reason="r"
        )

        store.remove(request_id)

        assert store.get(request_id) is None
        assert store.list() == []

    def test_remove_unknown_id_is_a_no_op(self, store):
        store.remove("does-not-exist")  # must not raise


class TestPersistenceAcrossInstances:
    """The whole point of file-backed storage: a new PendingRequestStore
    instance (e.g. after a server restart) sees what an earlier instance
    wrote.
    """

    def test_new_instance_sees_items_written_by_a_previous_one(self, tmp_path):
        path = tmp_path / "pending_requests.json"
        first = PendingRequestStore(path=path)
        request_id = first.add(
            sector="clinic", text="a", fields={}, skill_pack="clinic_v1", reason="r"
        )

        second = PendingRequestStore(path=path)

        assert second.get(request_id) is not None


class TestMissingOrCorruptFileTolerance:
    def test_missing_file_behaves_as_empty(self, tmp_path):
        store = PendingRequestStore(path=tmp_path / "does-not-exist.json")
        assert store.list() == []

    def test_corrupt_file_behaves_as_empty(self, tmp_path):
        path = tmp_path / "pending_requests.json"
        path.write_text("not valid json{{{", encoding="utf-8")
        store = PendingRequestStore(path=path)
        assert store.list() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pending.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eais_scheduling_agent.pending'`

- [ ] **Step 3: Write the implementation**

```python
"""File-backed queue of violation/conflict PENDING_APPROVAL requests
awaiting a human accept/reject decision (see
docs/superpowers/specs/2026-08-07-dashboard-redesign-design.md).

Not part of the assessment brief's scope -- see EXTENSIONS.md. Unlike
core.store.InMemoryBookingStore, this survives a server restart: it's a
JSON file (default pending_requests.json, gitignored), a dict keyed by
request id, rewritten in full on every mutation. Prototype-scale data (a
human review queue, not a high-volume log), so whole-file rewrite is
simple and sufficient -- no partial-write/append format needed.

Datetime handling: BookingRequest.fields sometimes holds a real datetime
under "start_time" (every current skill pack's convention -- see
core/audit.py's _json_safe docstring for the same assumption elsewhere
in this project). That value is serialized to an ISO 8601 string for the
JSON file and parsed back to a datetime on read, so a caller reconstructing
a BookingRequest from a stored item gets the same type slot_rules()
expects, not a string it would crash on.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union


class PendingRequestStore:
    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def add(self, sector: str, text: str, fields: dict, skill_pack: str, reason: str) -> str:
        """Persist a new pending item, return its id."""
        items = self._read()
        request_id = uuid.uuid4().hex
        items[request_id] = {
            "id": request_id,
            "sector": sector,
            "text": text,
            "fields": self._serialize_fields(fields),
            "skill_pack": skill_pack,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(items)
        return request_id

    def list(self, sector: Optional[str] = None) -> List[dict]:
        items = [self._deserialize_item(item) for item in self._read().values()]
        if sector is not None:
            items = [item for item in items if item["sector"] == sector]
        return items

    def get(self, request_id: str) -> Optional[dict]:
        items = self._read()
        raw = items.get(request_id)
        return self._deserialize_item(raw) if raw is not None else None

    def remove(self, request_id: str) -> None:
        items = self._read()
        items.pop(request_id, None)
        self._write(items)

    # -- internals --------------------------------------------------------

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write(self, items: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    @staticmethod
    def _serialize_fields(fields: dict) -> dict:
        serialized = dict(fields)
        if isinstance(serialized.get("start_time"), datetime):
            serialized["start_time"] = serialized["start_time"].isoformat()
        return serialized

    @staticmethod
    def _deserialize_item(item: dict) -> dict:
        result = dict(item)
        fields = dict(result["fields"])
        if isinstance(fields.get("start_time"), str):
            fields["start_time"] = datetime.fromisoformat(fields["start_time"])
        result["fields"] = fields
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pending.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add eais_scheduling_agent/pending.py tests/test_pending.py
git commit -m "Add PendingRequestStore: file-backed human accept/reject queue"
```

---

### Task 2: Sector-split audit files, backward-compatible

**Files:**
- Modify: `eais_scheduling_agent/http_api.py` (imports, `create_app()`, `GET /audit`)
- Test: `tests/test_http_api.py` (append new test class; existing `TestAuditEndpoint` must keep passing unmodified)

**Interfaces:**
- Consumes: `JsonLinesAuditTrail(path: Union[str, Path])` from `core/audit.py` (unchanged).
- Produces: `create_app()` builds `audit_by_sector: Dict[str, JsonLinesAuditTrail]` with keys `"clinic"`, `"restaurant"`, derived from the existing `audit_file` argument via a new `_sector_audit_path(base: Path, sector: str) -> Path` helper (e.g. `audit.jsonl` -> `audit.clinic.jsonl`). `GET /audit` gains an optional `?sector=` query param.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_http_api.py`:

```python
class TestPerSectorAuditFiles:
    def test_sector_filter_returns_only_that_sectors_records(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm, customer Jane Smith"},
        )

        clinic_response = client.get("/audit?sector=clinic")
        restaurant_response = client.get("/audit?sector=restaurant")

        clinic_records = clinic_response.get_json()["records"]
        restaurant_records = restaurant_response.get_json()["records"]
        assert len(clinic_records) == 1
        assert "John Doe" in clinic_records[0]["input"]
        assert len(restaurant_records) == 1
        assert "Jane Smith" in restaurant_records[0]["input"]

    def test_unknown_sector_returns_400(self, client):
        response = client.get("/audit?sector=veterinary")
        assert response.status_code == 400

    def test_records_are_actually_in_separate_files_on_disk(self, tmp_path):
        audit_path = tmp_path / "audit.jsonl"
        app = create_app(audit_file=str(audit_path))
        app.testing = True
        test_client = app.test_client()

        test_client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )

        assert (tmp_path / "audit.clinic.jsonl").is_file()
        assert not (tmp_path / "audit.restaurant.jsonl").is_file()

    def test_no_sector_param_still_returns_merged_chronological_list(self, client):
        # Backward compatibility: this is the existing
        # TestAuditEndpoint::test_returns_one_record_per_request behavior,
        # re-asserted here as a named regression guard for the sector split.
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm, customer Jane Smith"},
        )

        response = client.get("/audit")
        records = response.get_json()["records"]

        assert len(records) == 2
        assert records[0]["input"] == "Dr. A today at 10am, patient John Doe"
        assert records[1]["input"] == "table for 4 today at 6pm, customer Jane Smith"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_http_api.py -k TestPerSectorAuditFiles -v`
Expected: FAIL — `sector` filter has no effect yet, `audit.clinic.jsonl` doesn't exist yet (single `audit.jsonl` is written instead).

- [ ] **Step 3: Modify `http_api.py`**

Add near the top, after the existing imports (below `_CONFIRMED = "CONFIRMED"`):

```python
_SECTORS = ("clinic", "restaurant")


def _sector_audit_path(base: Path, sector: str) -> Path:
    """Derive a per-sector audit file path from the base `audit_file`.

    `audit.jsonl` -> `audit.clinic.jsonl`. A base with no suffix (e.g.
    `audit`) becomes `audit.clinic` -- still unambiguous, just without an
    extension. Keeps `create_app(audit_file=...)`'s existing single-path
    argument working unchanged (see tests/test_http_api.py's `client`
    fixture) while giving each sector a genuinely separate file.
    """
    return base.with_name(f"{base.stem}.{sector}{base.suffix}")


def _read_audit_records(path: Path) -> list:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
```

In `create_app()`, replace:

```python
    audit = JsonLinesAuditTrail(path=audit_file)
```

with:

```python
    audit_base = Path(audit_file)
    audit_by_sector = {
        sector: JsonLinesAuditTrail(path=str(_sector_audit_path(audit_base, sector)))
        for sector in _SECTORS
    }
```

In `post_booking()`, replace the `SchedulingAgentCore(...)` construction's `audit=audit,` line with:

```python
            audit=audit_by_sector.get(sector, audit_by_sector["clinic"]),
```

(Safe default: an unrecognized `sector` makes `core.handle()` raise `UnknownSectorError` at manifest-load time, step 1, before `self._audit.append()` is ever called -- see `core/orchestrator.py`'s `handle()` docstring, "Audit guarantee" -- so this fallback audit trail is constructed but never actually written to for that case.)

Replace the whole `get_audit()` function body with:

```python
    @app.get("/audit")
    def get_audit():
        sector = request.args.get("sector")
        if sector is not None and sector not in _SECTORS:
            return jsonify({"error": f"unknown sector: {sector!r}"}), 400

        if sector is not None:
            records = _read_audit_records(_sector_audit_path(audit_base, sector))
        else:
            records = []
            for s in _SECTORS:
                records.extend(_read_audit_records(_sector_audit_path(audit_base, s)))
            records.sort(key=lambda r: r["timestamp"])

        return jsonify({"records": records}), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: All PASS, including every pre-existing test in the file (this is the backward-compatibility check).

- [ ] **Step 5: Commit**

```bash
git add eais_scheduling_agent/http_api.py tests/test_http_api.py
git commit -m "Split audit trail into per-sector files, with a backward-compatible merged view"
```

---

### Task 3: Distinguish "needs clarification" from "needs human review"

**Files:**
- Modify: `eais_scheduling_agent/http_api.py` (`post_booking()`)
- Test: `tests/test_http_api.py`

**Interfaces:**
- Produces: `POST /bookings` response gains a third possible `status`: `"NEEDS_CLARIFICATION"` (missing-fields case). `"PENDING_APPROVAL"` is now reserved for violation/conflict cases only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_http_api.py`:

```python
class TestNeedsClarification:
    def test_missing_required_field_returns_needs_clarification(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "book me in with the doctor tomorrow"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "NEEDS_CLARIFICATION"
        assert "missing required field(s):" in body["reason"]

    def test_unknown_practitioner_is_still_pending_approval_not_clarification(self, client):
        response = client.post(
            "/bookings",
            json={
                "sector": "clinic",
                "text": "Dr. Chen today at 10am, patient John Doe",
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "PENDING_APPROVAL"
        assert "unknown practitioner" in body["reason"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_http_api.py -k TestNeedsClarification -v`
Expected: FAIL — `test_missing_required_field_returns_needs_clarification` fails because the response status is still `"PENDING_APPROVAL"`.

- [ ] **Step 3: Modify `http_api.py`**

Add near the other module constants:

```python
_MISSING_FIELDS_PREFIX = "missing required field(s): "
_PENDING_APPROVAL = "PENDING_APPROVAL"
_NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
```

Replace the final line of `post_booking()`:

```python
        return jsonify({"status": "PENDING_APPROVAL", "reason": decision.reason}), 200
```

with:

```python
        if decision.reason.startswith(_MISSING_FIELDS_PREFIX):
            return jsonify({"status": _NEEDS_CLARIFICATION, "reason": decision.reason}), 200

        return jsonify({"status": _PENDING_APPROVAL, "reason": decision.reason}), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add eais_scheduling_agent/http_api.py tests/test_http_api.py
git commit -m "Classify missing-fields PENDING_APPROVAL as NEEDS_CLARIFICATION"
```

---

### Task 4: Queue violation/conflict requests into `PendingRequestStore`, add `GET /pending`

**Files:**
- Modify: `eais_scheduling_agent/http_api.py`
- Test: `tests/test_http_api.py`

**Interfaces:**
- Consumes: `PendingRequestStore` from Task 1 (`add`, `list`).
- Produces: `GET /pending?sector=` — `{"items": [...]}`, each item shaped like `PendingRequestStore.list()`'s output plus a JSON-safe `fields` (datetime serialized) for the HTTP response.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_http_api.py`:

```python
class TestPendingQueueWrite:
    def test_violation_is_queued_and_missing_fields_is_not(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. Chen today at 10am, patient John Doe"},
        )
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "book me in with the doctor tomorrow"},
        )

        response = client.get("/pending")
        items = response.get_json()["items"]

        assert len(items) == 1
        assert "unknown practitioner" in items[0]["reason"]
        assert items[0]["sector"] == "clinic"

    def test_confirmed_booking_is_not_queued(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )

        response = client.get("/pending")
        assert response.get_json()["items"] == []

    def test_sector_filter(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. Chen today at 10am, patient John Doe"},
        )
        client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 99 today at 6pm, customer A"},
        )

        clinic_items = client.get("/pending?sector=clinic").get_json()["items"]
        restaurant_items = client.get("/pending?sector=restaurant").get_json()["items"]

        assert len(clinic_items) == 1
        assert len(restaurant_items) == 1
        assert clinic_items[0]["sector"] == "clinic"
        assert restaurant_items[0]["sector"] == "restaurant"

    def test_unknown_sector_filter_returns_400(self, client):
        response = client.get("/pending?sector=veterinary")
        assert response.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_http_api.py -k TestPendingQueueWrite -v`
Expected: FAIL — `/pending` doesn't exist yet (404).

- [ ] **Step 3: Modify `http_api.py`**

Add the import at the top, alongside the other `eais_scheduling_agent` imports:

```python
from eais_scheduling_agent.pending import PendingRequestStore
```

In `create_app()`'s signature, add a new parameter (default derived the same way audit files are, so callers who only pass `audit_file` still get a sane, colocated default):

```python
def create_app(
    manifest_dir: Union[str, Path] = wiring.DEFAULT_MANIFEST_DIR,
    audit_file: Union[str, Path] = "audit.jsonl",
    pending_file: Union[str, Path] = "pending_requests.json",
) -> Flask:
```

(Update the docstring's `Args:` block to add `pending_file: Path to the JSON file backing the human accept/reject queue (default: pending_requests.json, gitignored).`)

In `create_app()`'s body, alongside `runtime_config = _RuntimeLLMConfig()`:

```python
    pending_store = PendingRequestStore(path=pending_file)
```

Replace `post_booking()`'s final two branches (from `if decision.reason.startswith(...)` through the end of the function, as written in Task 3) with:

```python
        if decision.reason.startswith(_MISSING_FIELDS_PREFIX):
            return jsonify({"status": _NEEDS_CLARIFICATION, "reason": decision.reason}), 200

        manifest = wiring.load_manifest_for_render(manifest_dir, sector)
        booking_request = intake.parse(text, sector)  # cache hit
        pending_store.add(
            sector=sector,
            text=text,
            fields=booking_request.fields,
            skill_pack=manifest.skill_pack,
            reason=decision.reason,
        )
        return jsonify({"status": _PENDING_APPROVAL, "reason": decision.reason}), 200
```

Add the new endpoint, near `get_audit()`:

```python
    @app.get("/pending")
    def get_pending():
        sector = request.args.get("sector")
        if sector is not None and sector not in _SECTORS:
            return jsonify({"error": f"unknown sector: {sector!r}"}), 400

        items = pending_store.list(sector=sector)
        return jsonify({"items": [_pending_item_to_json(item) for item in items]}), 200
```

Add the JSON-safety helper near `_read_audit_records`:

```python
def _pending_item_to_json(item: dict) -> dict:
    """`PendingRequestStore.list()` returns real `datetime` objects inside
    `fields` (see that module's docstring) -- Flask's `jsonify` cannot
    serialize those directly, so this converts just that one field back
    to a string for the HTTP response.
    """
    result = dict(item)
    fields = dict(result["fields"])
    if isinstance(fields.get("start_time"), datetime):
        fields["start_time"] = fields["start_time"].isoformat()
    result["fields"] = fields
    return result
```

Add `from datetime import datetime` to the top-level imports if not already present (it is not, in the current file).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add eais_scheduling_agent/http_api.py tests/test_http_api.py
git commit -m "Queue violation/conflict requests into PendingRequestStore, add GET /pending"
```

---

### Task 5: `POST /pending/<id>/accept` and `POST /pending/<id>/reject`

**Files:**
- Modify: `eais_scheduling_agent/http_api.py`
- Test: `tests/test_http_api.py`

**Interfaces:**
- Consumes: `SkillPack.slot_rules(request) -> SlotInfo`, `BookingStore.check_conflict(request, slot) -> bool`, `BookingStore.persist(request, slot) -> None` (all existing, `core/interfaces.py` / `skillpacks/base.py`), `wiring.render_confirmation(skill_pack, request) -> str` (existing).
- Produces: `POST /pending/<id>/accept` -> `{"status": "CONFIRMED", "message": "..."}` (200) / `{"error": "..."}` (404 or 409). `POST /pending/<id>/reject` -> `{"status": "REJECTED"}` (200) / `{"error": "..."}` (404).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_http_api.py`:

```python
class TestAcceptPendingRequest:
    def _queue_one(self, client, text="Dr. Chen today at 10am, patient John Doe"):
        client.post("/bookings", json={"sector": "clinic", "text": text})
        return client.get("/pending").get_json()["items"][0]["id"]

    def test_accept_confirms_persists_and_removes_from_queue(self, client):
        request_id = self._queue_one(client)

        response = client.post(f"/pending/{request_id}/accept")

        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "CONFIRMED"
        assert "John Doe" in body["message"]
        assert client.get("/pending").get_json()["items"] == []

    def test_accept_writes_a_confirmed_audit_record(self, client):
        request_id = self._queue_one(client)
        client.post(f"/pending/{request_id}/accept")

        records = client.get("/audit?sector=clinic").get_json()["records"]

        confirmed = [r for r in records if r["approval_status"] == "approved"]
        assert len(confirmed) == 1
        assert confirmed[0]["decision"] == "CONFIRMED"

    def test_accept_unknown_id_returns_404(self, client):
        response = client.post("/pending/does-not-exist/accept")
        assert response.status_code == 404

    def test_accept_twice_returns_404_the_second_time(self, client):
        request_id = self._queue_one(client)
        client.post(f"/pending/{request_id}/accept")

        response = client.post(f"/pending/{request_id}/accept")

        assert response.status_code == 404

    def test_accept_a_now_conflicting_slot_returns_409_and_stays_pending(self, client):
        first_id = self._queue_one(client, text="Dr. Chen today at 10am, patient John Doe")
        second_id = self._queue_one(client, text="Dr. Chen today at 10am, patient Jane Roe")
        client.post(f"/pending/{first_id}/accept")  # takes the slot

        response = client.post(f"/pending/{second_id}/accept")

        assert response.status_code == 409
        assert client.get(f"/pending").get_json()["items"][0]["id"] == second_id


class TestRejectPendingRequest:
    def _queue_one(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. Chen today at 10am, patient John Doe"},
        )
        return client.get("/pending").get_json()["items"][0]["id"]

    def test_reject_removes_from_queue_and_persists_nothing(self, client):
        request_id = self._queue_one(client)

        response = client.post(f"/pending/{request_id}/reject")

        assert response.status_code == 200
        assert response.get_json()["status"] == "REJECTED"
        assert client.get("/pending").get_json()["items"] == []

    def test_reject_writes_a_rejected_audit_record(self, client):
        request_id = self._queue_one(client)
        client.post(f"/pending/{request_id}/reject")

        records = client.get("/audit?sector=clinic").get_json()["records"]

        rejected = [r for r in records if r["approval_status"] == "rejected"]
        assert len(rejected) == 1

    def test_reject_unknown_id_returns_404(self, client):
        response = client.post("/pending/does-not-exist/reject")
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_http_api.py -k "TestAcceptPendingRequest or TestRejectPendingRequest" -v`
Expected: FAIL — both endpoints 404 (routes don't exist yet).

- [ ] **Step 3: Modify `http_api.py`**

Change the `from datetime import datetime` line added in Task 4 to:

```python
from datetime import datetime, timezone
```

Add this import at the top, alongside the other `eais_scheduling_agent` imports:

```python
from eais_scheduling_agent.core.models import AuditRecord, BookingRequest
```

Add the two endpoints, after `get_pending()`:

```python
    @app.post("/pending/<request_id>/accept")
    def accept_pending(request_id):
        item = pending_store.get(request_id)
        if item is None:
            return jsonify({"error": f"no pending request with id {request_id!r}"}), 404

        booking_request = BookingRequest(
            sector=item["sector"], fields=item["fields"], raw_text=item["text"]
        )
        skill_pack = skill_packs[item["skill_pack"]]
        slot = skill_pack.slot_rules(booking_request)

        if store.check_conflict(booking_request, slot):
            return (
                jsonify({"error": "requested slot now conflicts with an existing booking"}),
                409,
            )

        store.persist(booking_request, slot)
        audit_by_sector[item["sector"]].append(
            AuditRecord(
                input=item["text"],
                skill_pack=item["skill_pack"],
                intent=dict(item["fields"]),
                rules_evaluated=[f"human override: accepted (was: {item['reason']})"],
                decision=_CONFIRMED,
                approval_status="approved",
                timestamp=datetime.now(timezone.utc),
            )
        )
        pending_store.remove(request_id)

        message = wiring.render_confirmation(skill_pack, booking_request)
        return jsonify({"status": _CONFIRMED, "message": message}), 200

    @app.post("/pending/<request_id>/reject")
    def reject_pending(request_id):
        item = pending_store.get(request_id)
        if item is None:
            return jsonify({"error": f"no pending request with id {request_id!r}"}), 404

        audit_by_sector[item["sector"]].append(
            AuditRecord(
                input=item["text"],
                skill_pack=item["skill_pack"],
                intent=dict(item["fields"]),
                rules_evaluated=[f"human override: rejected (was: {item['reason']})"],
                decision=_PENDING_APPROVAL,
                approval_status="rejected",
                timestamp=datetime.now(timezone.utc),
            )
        )
        pending_store.remove(request_id)

        return jsonify({"status": "REJECTED"}), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: All PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: All PASS (no regressions in any other module).

- [ ] **Step 6: Commit**

```bash
git add eais_scheduling_agent/http_api.py tests/test_http_api.py
git commit -m "Add POST /pending/<id>/accept and /reject"
```

---

### Task 6: `.gitignore` for the new per-sector/pending files

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add the new gitignored paths**

```
audit.clinic.jsonl
audit.restaurant.jsonl
pending_requests.json
```

Add these three lines directly below the existing `audit.jsonl` / `audit.eval.jsonl` lines.

- [ ] **Step 2: Verify**

Run: `git status --short` after starting the server once and making one booking of each kind — confirm `audit.clinic.jsonl`, `audit.restaurant.jsonl`, and `pending_requests.json` do **not** appear as untracked files.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "Gitignore the new per-sector audit files and pending-request queue"
```

---

### Task 7: Slot-rules config — read/edit practitioners, tables, and working hours

**Files:**
- Modify: `eais_scheduling_agent/skillpacks/clinic/pack.py` (add one read-only property)
- Modify: `eais_scheduling_agent/skillpacks/restaurant/pack.py` (add one read-only property)
- Modify: `eais_scheduling_agent/http_api.py` (two new import lines, four new endpoints)
- Test: `tests/test_clinic_skillpack.py`, `tests/test_restaurant_skillpack.py`, `tests/test_http_api.py`

**Context:** Added at Ahmed's explicit request, alongside the accept/reject queue work — a way to see and edit each sector's slot rules (clinic: which practitioners exist and their appointment duration; restaurant: which tables exist and their seat capacity; both: working hours) from the dashboard, shown alongside that sector's audit trail. Still no authentication, matching every other endpoint.

`ClinicSkillPack` and `RestaurantSkillPack` are otherwise immutable after construction (config is parsed once in `__init__`) — rather than adding mutation methods to those classes, this task adds one read-only property each (mirroring the existing `working_hours` property exactly), and does all *mutation* at the `http_api.py` layer by constructing a **new** skill pack instance with merged config and replacing the entry in the `skill_packs` dict `create_app()` already holds for the server's lifetime. This keeps every parsing/validation rule (e.g. `"HH:MM"` format checking) inside the already-tested skill pack constructors — `http_api.py` only orchestrates "merge, construct, swap, or reject with 400."

**Interfaces:**
- Produces on `ClinicSkillPack`: `practitioners` property, returns `Dict[str, int]` (a copy).
- Produces on `RestaurantSkillPack`: `tables` property, returns `Dict[str, int]` (a copy).
- Produces on `http_api.py`: `GET /config/clinic` -> `{"practitioners": {...}, "working_hours": {...}}`; `POST /config/clinic` (body: `{"practitioners": {...}}` and/or `{"working_hours": {...}}`, both optional) -> same shape, 200, or `{"error": "..."}`, 400. Same shape for `/config/restaurant` with `"tables"` instead of `"practitioners"`.
- Consumes: `wiring.build_skill_packs()`'s `skill_packs` dict (already built once in `create_app()`, mutated in place by these endpoints — every other endpoint that does `skill_packs[...]` lookups, e.g. `accept_pending` from Task 5, sees the update on its next call, since it's the same dict object).

- [ ] **Step 1: Write the failing tests for the skill pack properties**

Add to `tests/test_clinic_skillpack.py`, near the existing `test_working_hours_default`/`test_working_hours_configurable` tests:

```python
    def test_practitioners_default(self):
        pack = ClinicSkillPack()
        assert pack.practitioners == {"Dr. A": 30, "Dr. B": 20}

    def test_practitioners_configurable(self):
        pack = ClinicSkillPack(practitioners={"Dr. X": 45})
        assert pack.practitioners == {"Dr. X": 45}

    def test_practitioners_returns_a_copy_not_the_internal_dict(self):
        pack = ClinicSkillPack()
        pack.practitioners["Dr. Z"] = 99
        assert "Dr. Z" not in pack.practitioners
```

Add to `tests/test_restaurant_skillpack.py`, near the existing working-hours tests:

```python
    def test_tables_default(self):
        pack = RestaurantSkillPack()
        assert pack.tables == {"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}

    def test_tables_configurable(self):
        pack = RestaurantSkillPack(tables={"A1": 10})
        assert pack.tables == {"A1": 10}

    def test_tables_returns_a_copy_not_the_internal_dict(self):
        pack = RestaurantSkillPack()
        pack.tables["Z9"] = 99
        assert "Z9" not in pack.tables
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_clinic_skillpack.py tests/test_restaurant_skillpack.py -v`
Expected: FAIL with `AttributeError: 'ClinicSkillPack' object has no attribute 'practitioners'` (and the equivalent for `tables`).

- [ ] **Step 3: Add the properties**

In `eais_scheduling_agent/skillpacks/clinic/pack.py`, immediately after the existing `working_hours` property:

```python
    @property
    def practitioners(self) -> Dict[str, int]:
        """The clinic's configured practitioner -> fixed appointment length (minutes) mapping.

        Returns a copy of the ``{practitioner_name: duration_minutes}``
        dict as configured (or the default) -- mirrors `working_hours`'s
        read-only, defensive-copy convention.
        """
        return dict(self._practitioners)
```

In `eais_scheduling_agent/skillpacks/restaurant/pack.py`, immediately after the existing `working_hours` property:

```python
    @property
    def tables(self) -> Dict[str, int]:
        """The restaurant's configured table -> seat capacity mapping.

        Returns a copy of the ``{table_id: capacity}`` dict as configured
        (or the default) -- mirrors `working_hours`'s read-only,
        defensive-copy convention.
        """
        return dict(self._tables)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_clinic_skillpack.py tests/test_restaurant_skillpack.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add eais_scheduling_agent/skillpacks/clinic/pack.py eais_scheduling_agent/skillpacks/restaurant/pack.py tests/test_clinic_skillpack.py tests/test_restaurant_skillpack.py
git commit -m "Add read-only practitioners/tables properties to the clinic and restaurant skill packs"
```

- [ ] **Step 6: Write the failing tests for the config endpoints**

Append to `tests/test_http_api.py`:

```python
class TestClinicConfigEndpoint:
    def test_get_returns_current_practitioners_and_hours(self, client):
        response = client.get("/config/clinic")
        assert response.status_code == 200
        body = response.get_json()
        assert body["practitioners"] == {"Dr. A": 30, "Dr. B": 20}
        assert body["working_hours"] == {"open": "09:00", "close": "17:00"}

    def test_post_adds_a_new_practitioner(self, client):
        response = client.post("/config/clinic", json={"practitioners": {"Dr. C": 25}})
        assert response.status_code == 200
        body = response.get_json()
        assert body["practitioners"] == {"Dr. A": 30, "Dr. B": 20, "Dr. C": 25}

    def test_post_changes_an_existing_practitioners_duration(self, client):
        client.post("/config/clinic", json={"practitioners": {"Dr. A": 45}})
        response = client.get("/config/clinic")
        assert response.get_json()["practitioners"]["Dr. A"] == 45

    def test_post_new_practitioner_is_immediately_bookable(self, client):
        client.post("/config/clinic", json={"practitioners": {"Dr. C": 25}})

        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. C today at 10am, patient John Doe"},
        )

        assert response.get_json()["status"] == "CONFIRMED"

    def test_post_changes_working_hours(self, client):
        response = client.post(
            "/config/clinic", json={"working_hours": {"open": "08:00", "close": "12:00"}}
        )
        assert response.status_code == 200
        assert response.get_json()["working_hours"] == {"open": "08:00", "close": "12:00"}

    def test_post_rejects_non_object_body(self, client):
        response = client.post("/config/clinic", json="not an object")
        assert response.status_code == 400

    def test_post_rejects_non_dict_practitioners(self, client):
        response = client.post("/config/clinic", json={"practitioners": "Dr. C"})
        assert response.status_code == 400

    def test_post_rejects_non_positive_duration(self, client):
        response = client.post("/config/clinic", json={"practitioners": {"Dr. C": 0}})
        assert response.status_code == 400

    def test_post_with_empty_practitioners_object_is_a_no_op_merge(self, client):
        # Merge semantics (see Step 8): {} has nothing to update, so the
        # current config comes back unchanged -- not an error, since {}
        # is a valid "no additions" request, not an attempt to empty it.
        response = client.post("/config/clinic", json={"practitioners": {}})
        assert response.status_code == 200
        assert response.get_json()["practitioners"] == {"Dr. A": 30, "Dr. B": 20}

    def test_post_rejects_malformed_working_hours(self, client):
        response = client.post(
            "/config/clinic", json={"working_hours": {"open": "not-a-time", "close": "17:00"}}
        )
        assert response.status_code == 400


class TestRestaurantConfigEndpoint:
    def test_get_returns_current_tables_and_hours(self, client):
        response = client.get("/config/restaurant")
        assert response.status_code == 200
        body = response.get_json()
        assert body["tables"] == {"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}
        assert body["working_hours"] == {"open": "11:00", "close": "22:00"}

    def test_post_adds_a_new_table(self, client):
        response = client.post("/config/restaurant", json={"tables": {"T6": 12}})
        assert response.status_code == 200
        assert response.get_json()["tables"]["T6"] == 12

    def test_post_new_table_is_immediately_bookable(self, client):
        client.post("/config/restaurant", json={"tables": {"T6": 20}})

        response = client.post(
            "/bookings",
            json={
                "sector": "restaurant",
                "text": "table for 15 today at 6pm, customer Jane Smith",
            },
        )

        assert response.get_json()["status"] == "CONFIRMED"

    def test_post_rejects_non_positive_capacity(self, client):
        response = client.post("/config/restaurant", json={"tables": {"T6": -1}})
        assert response.status_code == 400
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `python -m pytest tests/test_http_api.py -k "TestClinicConfigEndpoint or TestRestaurantConfigEndpoint" -v`
Expected: FAIL — `/config/clinic` and `/config/restaurant` don't exist yet (404).

- [ ] **Step 8: Add the endpoints**

Add these imports at the top of `eais_scheduling_agent/http_api.py`, alongside the other `eais_scheduling_agent` imports:

```python
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack
```

Add the four endpoints, after `get_config()`/`post_config()` (the existing LLM-config endpoints):

```python
    @app.get("/config/clinic")
    def get_clinic_config():
        pack = skill_packs["clinic_v1"]
        return jsonify({"practitioners": pack.practitioners, "working_hours": pack.working_hours}), 200

    @app.post("/config/clinic")
    def post_clinic_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        current = skill_packs["clinic_v1"]
        practitioners = dict(current.practitioners)
        if "practitioners" in body:
            new_entries = body["practitioners"]
            valid = isinstance(new_entries, dict) and all(
                isinstance(name, str)
                and isinstance(minutes, int)
                and not isinstance(minutes, bool)
                and minutes > 0
                for name, minutes in new_entries.items()
            )
            if not valid:
                return (
                    jsonify(
                        {"error": "'practitioners' must be an object of name -> positive integer minutes"}
                    ),
                    400,
                )
            practitioners.update(new_entries)

        working_hours = dict(current.working_hours)
        if "working_hours" in body:
            if not isinstance(body["working_hours"], dict):
                return jsonify({"error": "'working_hours' must be an object with 'open'/'close'"}), 400
            working_hours = body["working_hours"]

        try:
            new_pack = ClinicSkillPack(practitioners=practitioners, working_hours=working_hours)
        except (ValueError, KeyError) as exc:
            return jsonify({"error": f"invalid config: {exc}"}), 400

        skill_packs["clinic_v1"] = new_pack
        return jsonify({"practitioners": new_pack.practitioners, "working_hours": new_pack.working_hours}), 200

    @app.get("/config/restaurant")
    def get_restaurant_config():
        pack = skill_packs["restaurant_v1"]
        return jsonify({"tables": pack.tables, "working_hours": pack.working_hours}), 200

    @app.post("/config/restaurant")
    def post_restaurant_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        current = skill_packs["restaurant_v1"]
        tables = dict(current.tables)
        if "tables" in body:
            new_entries = body["tables"]
            valid = isinstance(new_entries, dict) and all(
                isinstance(table_id, str)
                and isinstance(capacity, int)
                and not isinstance(capacity, bool)
                and capacity > 0
                for table_id, capacity in new_entries.items()
            )
            if not valid:
                return (
                    jsonify({"error": "'tables' must be an object of table id -> positive integer capacity"}),
                    400,
                )
            tables.update(new_entries)

        working_hours = dict(current.working_hours)
        if "working_hours" in body:
            if not isinstance(body["working_hours"], dict):
                return jsonify({"error": "'working_hours' must be an object with 'open'/'close'"}), 400
            working_hours = body["working_hours"]

        try:
            new_pack = RestaurantSkillPack(tables=tables, working_hours=working_hours)
        except (ValueError, KeyError) as exc:
            return jsonify({"error": f"invalid config: {exc}"}), 400

        skill_packs["restaurant_v1"] = new_pack
        return jsonify({"tables": new_pack.tables, "working_hours": new_pack.working_hours}), 200
```

(Note: `practitioners`/`tables` use a **merge** — `dict.update()` — semantics: posting `{"practitioners": {"Dr. C": 25}}` adds Dr. C without removing Dr. A/Dr. B, and posting `{"practitioners": {"Dr. A": 45}}` changes only Dr. A's duration. There is deliberately no way to remove a practitioner/table through this endpoint — matches Ahmed's request, "add practitioners or change durations," which is additive/corrective, not deletive. `working_hours`, by contrast, is a wholesale replace when provided, since a partial open/close makes no sense.)

- [ ] **Step 9: Run tests to verify they pass**

Run: `python -m pytest tests/test_http_api.py -v`
Expected: All PASS.

- [ ] **Step 10: Run the full suite**

Run: `python -m pytest -q`
Expected: All PASS.

- [ ] **Step 11: Commit**

```bash
git add eais_scheduling_agent/http_api.py tests/test_http_api.py
git commit -m "Add GET/POST /config/clinic and /config/restaurant for slot-rules editing"
```

---

### Task 8: Dashboard structure & style — tab bar, Warm Neutral palette

**Files:**
- Modify: `eais_scheduling_agent/templates/dashboard.html` (full rewrite)
- Modify: `eais_scheduling_agent/static/style.css` (full rewrite)

**Interfaces:**
- Consumes: `GET /config/clinic` / `GET /config/restaurant` (Task 7) for the read-only slot-rules markup below (populated by Task 9's JS, not this task).
- Produces: five `<section>` elements with `id="tab-book"`, `id="tab-pending"`, `id="tab-audit-clinic"`, `id="tab-audit-restaurant"`, `id="tab-config"`, toggled by Task 9's JS via a `.active` class on both the tab button and its section. No behavior in this task — verified visually only.

- [ ] **Step 1: Write `dashboard.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>EAIS Scheduling Agent</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
  <header class="app-header">
    <h1>EAIS Scheduling Agent</h1>
  </header>

  <nav class="tab-bar" id="tab-bar">
    <button class="tab-button active" data-tab="tab-book" type="button">Book</button>
    <button class="tab-button" data-tab="tab-pending" type="button">
      Pending <span class="badge" id="pending-badge">0</span>
    </button>
    <button class="tab-button" data-tab="tab-audit-clinic" type="button">Audit: Clinic</button>
    <button class="tab-button" data-tab="tab-audit-restaurant" type="button">Audit: Restaurant</button>
    <button class="tab-button" data-tab="tab-config" type="button">Config</button>
  </nav>

  <main>
    <section id="tab-book" class="tab-panel active">
      <h2>Make a booking</h2>
      <form id="booking-form">
        <label for="sector">Sector</label>
        <select id="sector" name="sector">
          <option value="clinic">Clinic</option>
          <option value="restaurant">Restaurant</option>
        </select>

        <label for="text">Request</label>
        <textarea id="text" name="text" rows="3" placeholder="Dr. A today at 10am, patient John Doe"></textarea>

        <label class="checkbox-label"><input type="checkbox" id="use-llm" name="llm"> Use LLM intake</label>

        <button type="submit">Book</button>
      </form>
      <div id="booking-result" class="result"></div>
    </section>

    <section id="tab-pending" class="tab-panel">
      <h2>Pending requests</h2>
      <p class="subtitle">Requests that are fully understood but need a human call -- unknown practitioner, over capacity, or a slot conflict.</p>
      <button type="button" id="refresh-pending">Refresh</button>
      <div id="pending-list" class="card-list"></div>
    </section>

    <section id="tab-audit-clinic" class="tab-panel">
      <h2>Audit trail &mdash; Clinic</h2>

      <div class="slot-rules">
        <h3>Slot rules</h3>
        <dl id="clinic-rules-display" class="rules-display"></dl>
        <button type="button" class="toggle-edit" data-target="clinic-rules-form">Edit</button>
        <form id="clinic-rules-form" class="rules-form hidden">
          <label for="clinic-practitioner-name">Practitioner name (new or existing)</label>
          <input type="text" id="clinic-practitioner-name" placeholder="Dr. C">

          <label for="clinic-practitioner-duration">Duration (minutes)</label>
          <input type="number" id="clinic-practitioner-duration" min="1" step="1">

          <label for="clinic-open">Open (HH:MM)</label>
          <input type="text" id="clinic-open" placeholder="09:00">

          <label for="clinic-close">Close (HH:MM)</label>
          <input type="text" id="clinic-close" placeholder="17:00">

          <button type="submit">Save</button>
        </form>
        <div id="clinic-rules-result" class="result"></div>
      </div>

      <button type="button" class="refresh-audit" data-sector="clinic">Refresh</button>
      <table class="audit-table">
        <thead>
          <tr><th>Time</th><th>Input</th><th>Decision</th><th>Approval</th></tr>
        </thead>
        <tbody id="audit-body-clinic"></tbody>
      </table>
    </section>

    <section id="tab-audit-restaurant" class="tab-panel">
      <h2>Audit trail &mdash; Restaurant</h2>

      <div class="slot-rules">
        <h3>Slot rules</h3>
        <dl id="restaurant-rules-display" class="rules-display"></dl>
        <button type="button" class="toggle-edit" data-target="restaurant-rules-form">Edit</button>
        <form id="restaurant-rules-form" class="rules-form hidden">
          <label for="restaurant-table-id">Table id (new or existing)</label>
          <input type="text" id="restaurant-table-id" placeholder="T6">

          <label for="restaurant-table-capacity">Capacity (seats)</label>
          <input type="number" id="restaurant-table-capacity" min="1" step="1">

          <label for="restaurant-open">Open (HH:MM)</label>
          <input type="text" id="restaurant-open" placeholder="11:00">

          <label for="restaurant-close">Close (HH:MM)</label>
          <input type="text" id="restaurant-close" placeholder="22:00">

          <button type="submit">Save</button>
        </form>
        <div id="restaurant-rules-result" class="result"></div>
      </div>

      <button type="button" class="refresh-audit" data-sector="restaurant">Refresh</button>
      <table class="audit-table">
        <thead>
          <tr><th>Time</th><th>Input</th><th>Decision</th><th>Approval</th></tr>
        </thead>
        <tbody id="audit-body-restaurant"></tbody>
      </table>
    </section>

    <section id="tab-config" class="tab-panel">
      <h2>Model config</h2>
      <form id="config-form">
        <label for="base-url">Base URL</label>
        <input type="text" id="base-url" name="base_url">

        <label for="model">Model</label>
        <input type="text" id="model" name="model">

        <label for="api-key">API key</label>
        <input type="password" id="api-key" name="api_key">
        <span id="api-key-hint" class="hint"></span>

        <label for="timeout">Timeout (seconds)</label>
        <input type="number" id="timeout" name="timeout" step="0.1">

        <button type="submit">Save</button>
      </form>
      <div id="config-result" class="result"></div>
    </section>
  </main>

  <script src="{{ url_for('static', filename='app.js') }}"></script>

  <footer>
    <p>Local prototype, no authentication. Not part of the EAIS-HR-2159-TA-01 assessment submission -- see EXTENSIONS.md.</p>
  </footer>
</body>
</html>
```

- [ ] **Step 2: Write `style.css`**

```css
:root {
  --bg: #fdf8f3;
  --card-bg: #fffefb;
  --border: #ecd9c4;
  --accent: #c2703d;
  --confirmed: #3f8a5c;
  --muted: #8a7863;
  --body-text: #3f342a;
  --clarify-text: #b5786a;
  --clarify-bg: #f6ece7;
  --radius: 10px;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--body-text);
  font-family: "Segoe UI", Georgia, sans-serif;
}

.app-header {
  padding: 20px 24px 0;
}

.app-header h1 {
  font-family: Georgia, serif;
  font-size: 22px;
  margin: 0 0 12px;
}

.tab-bar {
  display: flex;
  gap: 20px;
  padding: 0 24px;
  border-bottom: 1px solid var(--border);
}

.tab-button {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  font-size: 14px;
  font-family: inherit;
  padding: 10px 2px;
  cursor: pointer;
}

.tab-button.active {
  color: var(--accent);
  font-weight: 600;
  border-bottom-color: var(--accent);
}

.badge {
  display: inline-block;
  background: var(--accent);
  color: #fff;
  border-radius: 10px;
  padding: 1px 7px;
  font-size: 11px;
  margin-left: 4px;
}

main {
  max-width: 720px;
  margin: 0 auto;
  padding: 24px;
}

.tab-panel {
  display: none;
}

.tab-panel.active {
  display: block;
}

h2 {
  font-family: Georgia, serif;
  font-size: 18px;
  margin-top: 0;
}

.subtitle {
  color: var(--muted);
  font-size: 13px;
  margin-top: -8px;
}

form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
}

label {
  font-size: 13px;
  color: var(--muted);
}

.checkbox-label {
  display: flex;
  align-items: center;
  gap: 6px;
}

input[type="text"],
input[type="password"],
input[type="number"],
select,
textarea {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-family: inherit;
  font-size: 14px;
  color: var(--body-text);
}

button[type="submit"],
#refresh-pending,
.refresh-audit {
  align-self: flex-start;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 14px;
  cursor: pointer;
}

button:disabled {
  opacity: 0.6;
  cursor: default;
}

.result {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius);
  font-size: 14px;
  opacity: 0;
  transition: opacity 0.2s ease;
}

.result.visible {
  opacity: 1;
}

.result.status-confirmed {
  background: #eaf5ee;
  color: var(--confirmed);
  border: 1px solid var(--confirmed);
}

.result.status-pending {
  background: #fbeee3;
  color: var(--accent);
  border: 1px solid var(--accent);
}

.result.status-clarify {
  background: var(--clarify-bg);
  color: var(--clarify-text);
  border: 1px solid var(--clarify-text);
}

.result.status-error {
  background: #fbeee3;
  color: var(--accent);
  border: 1px solid var(--accent);
}

.card-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 12px;
}

.pending-card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
}

.pending-card .meta {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.pending-card .text {
  margin: 6px 0;
  font-size: 14px;
}

.pending-card .reason {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 10px;
}

.pending-card .actions {
  display: flex;
  gap: 8px;
}

.pending-card .accept,
.pending-card .reject {
  border-radius: 8px;
  font-size: 12px;
  padding: 6px 14px;
  cursor: pointer;
}

.pending-card .accept {
  background: var(--confirmed);
  color: #fff;
  border: none;
}

.pending-card .reject {
  background: #fff;
  color: var(--accent);
  border: 1px solid var(--accent);
}

.empty-state {
  color: var(--muted);
  font-size: 13px;
  margin-top: 12px;
}

.slot-rules {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 16px;
  margin-bottom: 16px;
}

.slot-rules h3 {
  font-family: Georgia, serif;
  font-size: 15px;
  margin: 0 0 8px;
}

.rules-display {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0 0 10px;
  font-size: 13px;
}

.rules-display dt {
  color: var(--muted);
}

.rules-display dd {
  margin: 0;
}

.toggle-edit {
  background: #fff;
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 8px;
  font-size: 12px;
  padding: 5px 12px;
  cursor: pointer;
}

.rules-form {
  margin-top: 10px;
}

.rules-form.hidden {
  display: none;
}

.audit-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 12px;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.audit-table th,
.audit-table td {
  text-align: left;
  padding: 8px 10px;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}

.audit-table th {
  color: var(--muted);
  font-weight: 600;
}

.hint {
  font-size: 12px;
  color: var(--muted);
}

footer {
  max-width: 720px;
  margin: 0 auto;
  padding: 0 24px 24px;
  font-size: 12px;
  color: var(--muted);
}
```

- [ ] **Step 2 (verification): start the server and confirm the tabs render**

Run:
```bash
python -c "from eais_scheduling_agent.http_api import run; run()"
```

Open `http://127.0.0.1:5000/` in a browser. Confirm: the tab bar shows all five tabs with "Book" active by default, the Warm Neutral palette is visible (cream background, terracotta active-tab underline), every tab button is clickable (clicking others won't switch panels yet -- that's Task 9), and each audit tab shows an empty "Slot rules" card above its (also empty) audit table, with an "Edit" button that toggles the (currently non-functional) form below it. Stop the server (Ctrl+C) when done.

- [ ] **Step 3: Commit**

```bash
git add eais_scheduling_agent/templates/dashboard.html eais_scheduling_agent/static/style.css
git commit -m "Rebuild dashboard structure: tab bar + Warm Neutral palette"
```

---

### Task 9: Dashboard behavior — tabs, 3-state booking messages, pending queue, split audit, config, slot rules

**Files:**
- Modify: `eais_scheduling_agent/static/app.js` (full rewrite)

**Interfaces:**
- Consumes every endpoint from Tasks 2-5 and 7: `GET/POST /bookings`, `GET /audit?sector=`, `GET /pending?sector=`, `POST /pending/<id>/accept`, `POST /pending/<id>/reject`, `GET/POST /config` (unchanged from before this plan), `GET/POST /config/clinic`, `GET/POST /config/restaurant`.

- [ ] **Step 1: Write `app.js`**

```js
document.addEventListener("DOMContentLoaded", () => {
  // -- Tab switching --------------------------------------------------
  const tabButtons = document.querySelectorAll(".tab-button");
  const tabPanels = document.querySelectorAll(".tab-panel");

  function activateTab(tabId) {
    for (const button of tabButtons) {
      button.classList.toggle("active", button.dataset.tab === tabId);
    }
    for (const panel of tabPanels) {
      panel.classList.toggle("active", panel.id === tabId);
    }
    if (tabId === "tab-pending") loadPending();
    if (tabId === "tab-audit-clinic") {
      loadAudit("clinic");
      loadClinicRules();
    }
    if (tabId === "tab-audit-restaurant") {
      loadAudit("restaurant");
      loadRestaurantRules();
    }
  }

  for (const button of tabButtons) {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  }

  // Disables `button` and swaps its label to `loadingLabel` for the
  // duration of `action()`, restoring the original label afterward.
  async function withLoading(button, loadingLabel, action) {
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = loadingLabel;
    try {
      return await action();
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }

  function flashResult(el, text, statusClass) {
    el.textContent = text;
    el.className = "result";
    void el.offsetWidth;
    el.classList.add(statusClass, "visible");
  }

  // -- Booking form -----------------------------------------------------
  const bookingForm = document.getElementById("booking-form");
  const bookingResult = document.getElementById("booking-result");
  const sectorSelect = document.getElementById("sector");
  const textInput = document.getElementById("text");
  const useLlmCheckbox = document.getElementById("use-llm");

  const STATUS_MESSAGES = {
    CONFIRMED: (body) => ({ text: body.message, cls: "status-confirmed" }),
    PENDING_APPROVAL: (body) => ({
      text: `Sent for human review: ${body.reason}`,
      cls: "status-pending",
    }),
    NEEDS_CLARIFICATION: (body) => ({
      text: `We couldn't quite process that -- ${body.reason.replace("missing required field(s): ", "missing: ")}. Try rephrasing with more detail.`,
      cls: "status-clarify",
    }),
  };

  bookingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = bookingForm.querySelector("button[type=submit]");

    await withLoading(submitButton, "Booking...", async () => {
      const response = await fetch("/bookings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sector: sectorSelect.value,
          text: textInput.value,
          llm: useLlmCheckbox.checked,
        }),
      });
      const body = await response.json();

      if (response.ok) {
        const rendered = STATUS_MESSAGES[body.status](body);
        flashResult(bookingResult, rendered.text, rendered.cls);
      } else {
        flashResult(bookingResult, `Error: ${body.error}`, "status-error");
      }

      if (document.getElementById("tab-pending").classList.contains("active")) {
        loadPending();
      }
      refreshPendingBadge();
    });
  });

  // -- Pending queue ------------------------------------------------------
  const pendingList = document.getElementById("pending-list");
  const pendingBadge = document.getElementById("pending-badge");
  const refreshPendingButton = document.getElementById("refresh-pending");

  async function refreshPendingBadge() {
    const response = await fetch("/pending");
    const body = await response.json();
    pendingBadge.textContent = body.items.length;
  }

  function renderPendingCard(item) {
    const card = document.createElement("div");
    card.className = "pending-card";
    card.innerHTML = `
      <div class="meta"></div>
      <div class="text"></div>
      <div class="reason"></div>
      <div class="actions">
        <button type="button" class="accept">Accept</button>
        <button type="button" class="reject">Reject</button>
      </div>
    `;
    card.querySelector(".meta").textContent = item.sector;
    card.querySelector(".text").textContent = `"${item.text}"`;
    card.querySelector(".reason").textContent = item.reason;

    card.querySelector(".accept").addEventListener("click", async (event) => {
      await withLoading(event.target, "Accepting...", async () => {
        const response = await fetch(`/pending/${item.id}/accept`, { method: "POST" });
        const body = await response.json();
        if (!response.ok) {
          alert(`Could not accept: ${body.error}`);
        }
        await loadPending();
        await refreshPendingBadge();
      });
    });

    card.querySelector(".reject").addEventListener("click", async (event) => {
      await withLoading(event.target, "Rejecting...", async () => {
        await fetch(`/pending/${item.id}/reject`, { method: "POST" });
        await loadPending();
        await refreshPendingBadge();
      });
    });

    return card;
  }

  async function loadPending() {
    const response = await fetch("/pending");
    const body = await response.json();
    pendingList.innerHTML = "";
    if (body.items.length === 0) {
      pendingList.innerHTML = '<p class="empty-state">Nothing pending.</p>';
      return;
    }
    for (const item of body.items) {
      pendingList.appendChild(renderPendingCard(item));
    }
  }

  refreshPendingButton.addEventListener("click", () => {
    withLoading(refreshPendingButton, "Refreshing...", loadPending);
  });

  // -- Audit tabs (per sector) --------------------------------------------
  async function loadAudit(sector) {
    const response = await fetch(`/audit?sector=${sector}`);
    const body = await response.json();
    const tbody = document.getElementById(`audit-body-${sector}`);
    tbody.innerHTML = "";
    for (const record of body.records) {
      const row = document.createElement("tr");
      row.innerHTML = "<td></td><td></td><td></td><td></td>";
      row.children[0].textContent = record.timestamp;
      row.children[1].textContent = record.input;
      row.children[2].textContent = record.decision;
      row.children[3].textContent = record.approval_status;
      tbody.appendChild(row);
    }
  }

  for (const button of document.querySelectorAll(".refresh-audit")) {
    button.addEventListener("click", () => {
      withLoading(button, "Refreshing...", () => loadAudit(button.dataset.sector));
    });
  }

  // -- Slot rules (per sector) ---------------------------------------------
  function renderRulesDisplay(dl, items, workingHours) {
    dl.innerHTML = "";
    for (const [name, value] of Object.entries(items)) {
      const dt = document.createElement("dt");
      dt.textContent = name;
      const dd = document.createElement("dd");
      dd.textContent = value;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    const hoursDt = document.createElement("dt");
    hoursDt.textContent = "Hours";
    const hoursDd = document.createElement("dd");
    hoursDd.textContent = `${workingHours.open}–${workingHours.close}`;
    dl.appendChild(hoursDt);
    dl.appendChild(hoursDd);
  }

  async function loadClinicRules() {
    const response = await fetch("/config/clinic");
    const body = await response.json();
    const items = {};
    for (const [name, minutes] of Object.entries(body.practitioners)) {
      items[name] = `${minutes} min`;
    }
    renderRulesDisplay(document.getElementById("clinic-rules-display"), items, body.working_hours);
  }

  async function loadRestaurantRules() {
    const response = await fetch("/config/restaurant");
    const body = await response.json();
    const items = {};
    for (const [tableId, capacity] of Object.entries(body.tables)) {
      items[tableId] = `${capacity} seats`;
    }
    renderRulesDisplay(
      document.getElementById("restaurant-rules-display"),
      items,
      body.working_hours
    );
  }

  for (const toggle of document.querySelectorAll(".toggle-edit")) {
    toggle.addEventListener("click", () => {
      document.getElementById(toggle.dataset.target).classList.toggle("hidden");
    });
  }

  const clinicRulesForm = document.getElementById("clinic-rules-form");
  const clinicRulesResult = document.getElementById("clinic-rules-result");

  clinicRulesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = clinicRulesForm.querySelector("button[type=submit]");
    const name = document.getElementById("clinic-practitioner-name").value.trim();
    const duration = document.getElementById("clinic-practitioner-duration").value;
    const open = document.getElementById("clinic-open").value.trim();
    const close = document.getElementById("clinic-close").value.trim();

    const payload = {};
    if (name !== "" && duration !== "") {
      payload.practitioners = { [name]: Number(duration) };
    }
    if (open !== "" && close !== "") {
      payload.working_hours = { open, close };
    }

    await withLoading(submitButton, "Saving...", async () => {
      const response = await fetch("/config/clinic", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(clinicRulesResult, "Saved.", "status-confirmed");
        clinicRulesForm.reset();
        await loadClinicRules();
      } else {
        flashResult(clinicRulesResult, `Error: ${body.error}`, "status-error");
      }
    });
  });

  const restaurantRulesForm = document.getElementById("restaurant-rules-form");
  const restaurantRulesResult = document.getElementById("restaurant-rules-result");

  restaurantRulesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = restaurantRulesForm.querySelector("button[type=submit]");
    const tableId = document.getElementById("restaurant-table-id").value.trim();
    const capacity = document.getElementById("restaurant-table-capacity").value;
    const open = document.getElementById("restaurant-open").value.trim();
    const close = document.getElementById("restaurant-close").value.trim();

    const payload = {};
    if (tableId !== "" && capacity !== "") {
      payload.tables = { [tableId]: Number(capacity) };
    }
    if (open !== "" && close !== "") {
      payload.working_hours = { open, close };
    }

    await withLoading(submitButton, "Saving...", async () => {
      const response = await fetch("/config/restaurant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(restaurantRulesResult, "Saved.", "status-confirmed");
        restaurantRulesForm.reset();
        await loadRestaurantRules();
      } else {
        flashResult(restaurantRulesResult, `Error: ${body.error}`, "status-error");
      }
    });
  });

  // -- Config -------------------------------------------------------------
  const configForm = document.getElementById("config-form");
  const configResult = document.getElementById("config-result");
  const baseUrlInput = document.getElementById("base-url");
  const modelInput = document.getElementById("model");
  const apiKeyInput = document.getElementById("api-key");
  const apiKeyHint = document.getElementById("api-key-hint");
  const timeoutInput = document.getElementById("timeout");

  async function loadConfig() {
    const response = await fetch("/config");
    const body = await response.json();
    baseUrlInput.value = body.base_url;
    modelInput.value = body.model;
    timeoutInput.value = body.timeout;
    apiKeyHint.textContent = body.api_key_set
      ? "(already set -- leave blank to keep)"
      : "(not set)";
  }

  configForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = configForm.querySelector("button[type=submit]");

    await withLoading(submitButton, "Saving...", async () => {
      const payload = {
        base_url: baseUrlInput.value,
        model: modelInput.value,
        timeout: timeoutInput.value === "" ? null : Number(timeoutInput.value),
      };
      if (apiKeyInput.value !== "") {
        payload.api_key = apiKeyInput.value;
      }

      const response = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(configResult, "Saved.", "status-confirmed");
        apiKeyInput.value = "";
        await loadConfig();
      } else {
        flashResult(configResult, `Error: ${body.error}`, "status-error");
      }
    });
  });

  refreshPendingBadge();
  loadConfig();
});
```

- [ ] **Step 2 (verification): manually exercise every flow**

Run:
```bash
python -c "from eais_scheduling_agent.http_api import run; run()"
```

With the server running, in a browser at `http://127.0.0.1:5000/`:

1. **Book tab**: submit `Dr. A today at 10am, patient John Doe` → green "Confirmed:..." message. Submit `book me in with the doctor tomorrow` → rose "We couldn't quite process that -- missing: practitioner, patient_name..." message. Submit `Dr. Chen today at 10am, patient John Doe` → terracotta "Sent for human review: unknown practitioner: 'Dr. Chen'" message, and the Pending tab's badge count increases by 1.
2. **Pending tab**: the "Dr. Chen" card appears with its text and reason. Click Accept → card disappears, badge decrements. Verify with `curl http://127.0.0.1:5000/audit?sector=clinic` that a new record with `"approval_status": "approved"` and `"decision": "CONFIRMED"` was written.
3. Submit another unknown-practitioner request, click Reject this time → card disappears; verify via curl that the corresponding audit record has `"approval_status": "rejected"`.
4. **Audit: Clinic** and **Audit: Restaurant** tabs: confirm each only shows that sector's records (book one of each sector first if the tables look empty).
5. **Config tab**: unchanged from before this plan -- confirm it still loads/saves correctly.
6. **Slot rules**: on the Audit: Clinic tab, confirm the "Slot rules" card shows "Dr. A: 30 min", "Dr. B: 20 min", "Hours: 09:00–17:00". Click Edit, enter practitioner name "Dr. C", duration "25", leave hours blank, submit -- the card should refresh to show Dr. C added, without losing Dr. A/Dr. B. Submit a booking for "Dr. C today at 10am, patient Test Person" on the Book tab and confirm it's CONFIRMED (proves the new practitioner is real, not just displayed). Repeat the table-id/capacity equivalent on the Audit: Restaurant tab.

Stop the server (Ctrl+C) when done.

- [ ] **Step 3: Run the full backend test suite once more (frontend changes don't touch Python, but confirms nothing else broke)**

Run: `python -m pytest -q`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add eais_scheduling_agent/static/app.js
git commit -m "Wire up dashboard behavior: tabs, pending accept/reject, split audit, 3-state booking messages"
```

---

### Task 10: Update `EXTENSIONS.md` and `README.md`

**Files:**
- Modify: `EXTENSIONS.md`
- Modify: `README.md`

- [ ] **Step 1: Update `EXTENSIONS.md`'s "2. Web UI" section**

Add a new paragraph at the end of that section (after the existing "Known limitation" paragraph, before the "See `docs/superpowers/specs/...`" line), and update that doc-pointer line:

```markdown
**2026-08-07 redesign:** rebuilt around a tab bar (Book | Pending | Audit:
Clinic | Audit: Restaurant | Config) with a Warm Neutral visual style.
Two new capabilities: (1) `PENDING_APPROVAL` requests with a complete,
understood booking (unknown practitioner, over capacity, conflict) are
now queued in a file-backed `PendingRequestStore`
(`eais_scheduling_agent/pending.py`) for a human to accept or reject on
the new Pending tab -- accepting replays the exact
`slot_rules()`/`check_conflict()`/`persist()` sequence
`core/orchestrator.py` already uses internally, so acceptance can never
diverge from the core's own decision logic; (2) requests where intake
couldn't extract enough (`missing required field(s): ...`) are now a
distinct `NEEDS_CLARIFICATION` response instead of being lumped in with
`PENDING_APPROVAL` -- there's no complete booking to review in that case,
just an inline message asking for more detail. The audit trail is also
now genuinely split into `audit.clinic.jsonl` / `audit.restaurant.jsonl`
(web server only -- the CLI's `audit.jsonl` is unaffected), with
`GET /audit`'s existing no-argument merged view kept for backward
compatibility. (3) Each sector's slot rules (clinic: practitioners and
their appointment duration; restaurant: tables and their seat capacity;
both: working hours) are now viewable and editable from that sector's
audit tab via `GET/POST /config/clinic` and `GET/POST /config/restaurant`
-- additive/corrective only (add a practitioner, change a duration), no
deletion. `ClinicSkillPack`/`RestaurantSkillPack` gained one read-only
property each (`practitioners`/`tables`, mirroring the existing
`working_hours` property); all mutation happens by constructing a new
skill pack instance and swapping it into the `skill_packs` dict
`create_app()` already holds, not by adding setters to those classes.

See `docs/superpowers/specs/2026-08-06-web-ui-design.md` for the original
design rationale and `docs/superpowers/specs/2026-08-07-dashboard-redesign-design.md`
for this redesign's.
```

Remove the old final line (`See docs/superpowers/specs/2026-08-06-web-ui-design.md for the full design rationale.`) since it's now folded into the paragraph above.

- [ ] **Step 2: Update `README.md`'s "Web dashboard (optional)" section**

Replace the section body (keep the `## Web dashboard (optional)` heading) with:

```markdown
With the server running (see above), open `http://127.0.0.1:5000/` in a
browser. Five tabs:

- **Book** -- make a booking. Three possible outcomes, each styled
  distinctly: Confirmed (green), sent for human review (terracotta,
  violation/conflict cases), or needs clarification (rose, when intake
  couldn't extract enough from the text -- an inline message, not queued
  anywhere).
- **Pending** -- requests with a complete, understood booking that needs
  a human's judgment call (unknown practitioner, over capacity, slot
  conflict). Accept persists it as a real confirmed booking; Reject
  discards it. Both are logged to the audit trail. Survives a server
  restart (file-backed, unlike the in-memory booking store).
- **Audit: Clinic** / **Audit: Restaurant** -- genuinely separate audit
  files per sector (`audit.clinic.jsonl` / `audit.restaurant.jsonl`), each
  with a "Slot rules" card above the audit table showing that sector's
  practitioners (clinic) or tables (restaurant) and working hours, with
  an Edit form to add a practitioner/table or change a duration/capacity
  or the working hours. Additive/corrective only -- no way to remove an
  existing practitioner or table from this UI.
- **Config** -- view or change the LLM backend (`base_url`/`model`/
  `api_key`/`timeout`) at runtime, without restarting the server. The API
  key is never sent back to the browser as its raw value, only whether
  one is currently set.

Not part of the assessment brief's scope -- see `EXTENSIONS.md`.
```

- [ ] **Step 3: Commit**

```bash
git add EXTENSIONS.md README.md
git commit -m "Document the dashboard redesign in EXTENSIONS.md and README"
```

---

## Self-Review Notes (already applied above)

- **Spec coverage:** visual redesign (Task 8), per-sector audit split (Task 2), pending accept/reject queue (Tasks 1, 4, 5), clarification messaging (Task 3), slot-rules read/edit (Task 7, added mid-execution at Ahmed's request alongside Task 5's review), error handling (404/409/422 in Task 5, 400 in Task 7, missing/corrupt-file tolerance in Task 1), testing (every backend task is TDD; Task 9's frontend is manually verified, consistent with this project having no JS test framework yet). All covered.
- **Backward compatibility:** `create_app(audit_file=...)` single-arg construction, and `GET /audit` with no `sector` param, are both explicitly tested in Task 2 as regression guards, not just assumed.
- **Type/name consistency checked:** `PendingRequestStore.add/list/get/remove` signatures in Task 1 match every call site in Tasks 4-5; `_sector_audit_path`, `_read_audit_records`, `_pending_item_to_json` are each defined once (Tasks 2 and 4) and reused, not redefined; `skill_packs["clinic_v1"]`/`["restaurant_v1"]` replacement in Task 7 uses the exact same dict object every other endpoint (e.g. Task 5's `accept_pending`) already reads from, so no wiring changes were needed elsewhere for the swap to take effect.
- **Task 5 addendum:** a real defect in this plan's own test data was found and fixed during Task 5's execution (`ClinicSkillPack.slot_rules()` raises `ValueError` for an unknown practitioner, which the brief's original accept/409 test scenarios used) -- see the SDD ledger for the full account. Not a gap in the final shipped code, which was corrected and re-reviewed clean, but worth knowing if this plan is ever reused as a template.
