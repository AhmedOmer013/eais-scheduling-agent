"""Optional HTTP interface for the scheduling agent.

A thin Flask wrapper around the same `SchedulingAgentCore` the CLI (T14)
uses -- no new decision logic lives here; `core/` is untouched by this
module. See docs/superpowers/specs/2026-08-05-http-interface-design.md
for the full design rationale.

The one deliberate behavioral difference from the CLI: `create_app()`
builds `store`, `gate`, `audit`, `skill_packs`, and `runtime_config`
once, at app-creation time, and holds them for the server's lifetime.
The CLI builds a fresh `InMemoryBookingStore` per process (per
invocation), so two separate `eais-book` calls can never conflict with
each other; a long-running server is one continuous process, so sharing
one store here makes cross-request conflict detection real for as long
as the server runs (see `tests/test_http_api.py::TestSharedStoreAcrossRequests`).
That reasoning assumes single-request-at-a-time handling: the dev server
(`run()` below) runs threaded by default, and `InMemoryBookingStore` has
no internal locking, so two truly concurrent requests are not guaranteed
to serialize correctly (a check-then-act race is possible) -- a known,
documented limitation of this prototype, not something this module
guards against with a mutex. `runtime_config` (a `_RuntimeLLMConfig`)
holds the in-memory LLM backend override set via `POST /config`; it
starts with every field unset (falling back to `wiring.resolve_llm_config()`)
and is read by `POST /bookings`' `use_llm` branch on every request.

Besides `POST /bookings` and `GET /audit`, this module also serves the
dashboard itself (`GET /`, `eais_scheduling_agent/templates/dashboard.html`,
plus its static assets) and the runtime LLM config endpoints
(`GET /config`, `POST /config`) described above.

Intake and `SchedulingAgentCore` are built fresh *per request*, inside
`post_booking()`, wired to those same shared `store`/`gate`/`audit`/
`skill_packs` instances -- deliberately not once at app-creation time.
`SchedulingAgentCore.handle()` has no per-call way to select intake
mode, so a request's `"llm"` flag picks whether that request's core
wraps `OfflineIntake` or `LLMIntake` (with `OfflineIntake` fallback).
The per-request intake is still wrapped in `wiring.CachingIntake` so the
second `intake.parse()` call this module makes to render a CONFIRMED
message is a cache hit against the exact call `core.handle()` already
made within the same request -- but the cache itself is scoped to one
request and discarded afterward. This avoids two problems a
server-lifetime cache would have: unbounded memory growth, and stale
results -- `OfflineIntake` resolves relative language ("today at 10am")
against wall-clock *now*, so a server-lifetime cache could silently
replay a *previous* day's resolved date for identical request text
submitted again after midnight.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from flask import Flask, jsonify, render_template, request

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.gate import StandardApprovalGate
from eais_scheduling_agent.core.models import AuditRecord, BookingRequest
from eais_scheduling_agent.core.orchestrator import (
    InvalidManifestError,
    OrchestrationError,
    SchedulingAgentCore,
    SectorDisabledError,
    UnknownSectorError,
    UnknownSkillPackError,
)
from eais_scheduling_agent.core.store import InMemoryBookingStore
from eais_scheduling_agent.intake.llm import LLMIntake, OpenAICompatibleHTTPClient
from eais_scheduling_agent.intake.offline import OfflineIntake
from eais_scheduling_agent.pending import PendingRequestStore
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack

_CONFIRMED = "CONFIRMED"
_MISSING_FIELDS_PREFIX = "missing required field(s): "
_PENDING_APPROVAL = "PENDING_APPROVAL"
_NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"

# Must stay in sync with the manifest files under
# eais_scheduling_agent/manifests/ (one <sector>.yaml per sector here).
# Several places below assume every value in _SECTORS has both a manifest
# and an audit-by-sector entry: post_booking()'s `audit_by_sector.get(sector,
# audit_by_sector["clinic"])` fallback, and accept_pending()/reject_pending()'s
# `skill_packs`/`audit_by_sector` lookups keyed by the item's stored sector.
_SECTORS = ("clinic", "restaurant")

# Human-readable labels for the raw field names core/gate.py's missing-
# fields reason names (e.g. "patient_name"). core/ itself has no sector
# vocabulary to draw friendly names from by design (see
# core/interfaces.py's RuleContext docstring), so this translation lives
# here, at the one place that already knows both the raw reason and the
# sector's skill pack. A name absent from this map (future field) falls
# back to its raw key rather than raising -- see _friendly_field_name.
_FRIENDLY_FIELD_NAMES = {
    "practitioner": "doctor's name",
    "patient_name": "patient's name",
    "party_size": "party size",
    "customer_name": "name on the booking",
    "start_time": "timing",
}


def _friendly_field_name(raw: str) -> str:
    return _FRIENDLY_FIELD_NAMES.get(raw, raw)


def _join_with_and(items):
    """["a"] -> "a"; ["a","b"] -> "a and b"; ["a","b","c"] -> "a, b, and c"."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _build_clarification_message(sector: str, required_fields, raw_reason: str) -> str:
    """Turn core/gate.py's raw "missing required field(s): x, y" reason
    into a friendly, sector-specific message: what this sector's bookings
    need in plain language, and exactly which of those is actually
    missing -- not the full required set redundantly, and not raw field
    keys like "patient_name".
    """
    missing_raw = raw_reason[len(_MISSING_FIELDS_PREFIX):].split(", ")
    missing_friendly = [_friendly_field_name(f) for f in missing_raw]
    required_friendly = [_friendly_field_name(f) for f in required_fields]
    return (
        f"{sector.capitalize()} bookings need {_join_with_and(required_friendly)} "
        f"-- missing: {_join_with_and(missing_friendly)}."
    )


def _sector_audit_path(base: Path, sector: str) -> Path:
    """Derive a per-sector audit file path from the base `audit_file`.

    `audit.jsonl` -> `audit.clinic.jsonl`. A base with no suffix (e.g.
    `audit`) becomes `audit.clinic` -- still unambiguous, just without an
    extension. Keeps `create_app(audit_file=...)`'s existing single-path
    argument working unchanged (see tests/test_http_api.py's `client`
    fixture) while giving each sector a genuinely separate file.
    """
    return base.with_name(f"{base.stem}.{sector}{base.suffix}")


def _to_uae_display(iso_timestamp: str) -> str:
    """Convert an aware ISO 8601 timestamp string to UAE (UTC+4) time for
    display. `.astimezone()` correctly re-expresses a timestamp already
    in *any* offset -- both the UTC ones `core/orchestrator.py` writes
    (untouched -- see this module's own scope boundary) and the
    already-UAE ones `accept_pending`/`reject_pending` write below end
    up shown identically, in UAE time, regardless of which wrote them.
    """
    return datetime.fromisoformat(iso_timestamp).astimezone(wiring.UAE_TZ).isoformat()


def _read_audit_records(path: Path) -> list:
    """Read one JSON record per non-blank line, with its `timestamp`
    converted to UAE time for display (see `_to_uae_display`).

    Tolerates a single corrupt/truncated line (e.g. the server process
    was killed mid-write, leaving the last line half-written): that line
    is skipped, not treated as a reason to fail the whole read, since
    every other line is still valid JSON and GET /audit should keep
    showing them rather than 500ing for the entire dashboard.
    """
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "timestamp" in record:
            record["timestamp"] = _to_uae_display(record["timestamp"])
        records.append(record)
    return records


def _pending_item_to_json(item: dict) -> dict:
    """`PendingRequestStore.list()` returns real `datetime` objects inside
    `fields` (see that module's docstring) -- Flask's `jsonify` cannot
    serialize those directly, so this converts just that one field back
    to a string for the HTTP response. `created_at` is also converted to
    UAE time for display (see `_to_uae_display`).
    """
    result = dict(item)
    result["created_at"] = _to_uae_display(result["created_at"])
    fields = dict(result["fields"])
    if isinstance(fields.get("start_time"), datetime):
        fields["start_time"] = fields["start_time"].isoformat()
    result["fields"] = fields
    return result


class _RuntimeLLMConfig:
    """In-memory override for the LLM backend config, settable via
    `POST /config`. Each field is `None` until explicitly set, meaning
    "no override -- use `wiring.resolve_llm_config()`'s value for this
    field." Lives for the server process's lifetime, same scope as the
    shared `store`/`gate`/`audit` `create_app()` already holds.
    """

    def __init__(self) -> None:
        self.base_url: Optional[str] = None
        self.model: Optional[str] = None
        self.api_key: Optional[str] = None
        self.timeout: Optional[float] = None

    def effective(self) -> dict:
        """Merge this override on top of `wiring.resolve_llm_config()`."""
        base = wiring.resolve_llm_config()
        return {
            "base_url": self.base_url if self.base_url is not None else base["base_url"],
            "model": self.model if self.model is not None else base["model"],
            "api_key": self.api_key if self.api_key is not None else base["api_key"],
            "timeout": self.timeout if self.timeout is not None else base["timeout"],
        }


def create_app(
    manifest_dir: Union[str, Path] = wiring.DEFAULT_MANIFEST_DIR,
    audit_file: Union[str, Path] = "audit.jsonl",
    pending_file: Union[str, Path] = "pending_requests.json",
) -> Flask:
    """Build a Flask app with a shared store/gate/audit for its whole lifetime.

    Args:
        manifest_dir: Directory holding one `<sector>.yaml` manifest per
            sector (default: the package's bundled manifests directory,
            same default `eais-book` uses).
        audit_file: Base path used to derive each sector's own JSON Lines
            audit file (default: `audit.jsonl`, already git-ignored --
            same default `eais-book` uses). Not written to directly:
            `POST /bookings`/`POST /pending/<id>/accept`/
            `POST /pending/<id>/reject` append to, and `GET /audit` reads
            from, the per-sector file `_sector_audit_path` derives from
            this base (e.g. `audit.jsonl` -> `audit.clinic.jsonl`,
            `audit.restaurant.jsonl`) -- see that function's docstring.
        pending_file: Path to the JSON file backing the human accept/reject
            queue (default: pending_requests.json, gitignored).
    """
    app = Flask(__name__)

    skill_packs = wiring.build_skill_packs()
    gate = StandardApprovalGate()
    store = InMemoryBookingStore()
    audit_base = Path(audit_file)
    audit_by_sector = {
        sector: JsonLinesAuditTrail(path=str(_sector_audit_path(audit_base, sector)))
        for sector in _SECTORS
    }
    runtime_config = _RuntimeLLMConfig()
    pending_store = PendingRequestStore(path=pending_file)

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.post("/bookings")
    def post_booking():
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or "sector" not in body
            or "text" not in body
            or not isinstance(body["sector"], str)
            or not isinstance(body["text"], str)
        ):
            return (
                jsonify(
                    {
                        "error": "request body must be a JSON object with string "
                        "'sector' and 'text'"
                    }
                ),
                400,
            )

        sector = body["sector"]
        text = body["text"]
        use_llm = body.get("llm") is True

        if use_llm:
            client = OpenAICompatibleHTTPClient(**runtime_config.effective())
            intake = wiring.CachingIntake(
                LLMIntake(
                    fallback=OfflineIntake(now=wiring.uae_now),
                    client=client,
                    now=wiring.uae_now,
                )
            )
        else:
            intake = wiring.CachingIntake(OfflineIntake(now=wiring.uae_now))

        core = SchedulingAgentCore(
            manifest_dir=manifest_dir,
            skill_packs=skill_packs,
            intake=intake,
            gate=gate,
            store=store,
            audit=audit_by_sector.get(sector, audit_by_sector["clinic"]),
        )

        try:
            decision = core.handle(text, sector)
        except UnknownSectorError as exc:
            return jsonify({"error": str(exc)}), 404
        except SectorDisabledError as exc:
            return jsonify({"error": str(exc)}), 400
        except (InvalidManifestError, UnknownSkillPackError) as exc:
            return jsonify({"error": str(exc)}), 500
        except OrchestrationError as exc:
            return jsonify({"error": str(exc)}), 500

        if decision.status == _CONFIRMED:
            manifest = wiring.load_manifest_for_render(manifest_dir, sector)
            skill_pack = skill_packs[manifest.skill_pack]
            booking_request = intake.parse(text, sector)  # cache hit
            message = wiring.render_confirmation(skill_pack, booking_request)
            return jsonify({"status": _CONFIRMED, "message": message}), 200

        if decision.reason.startswith(_MISSING_FIELDS_PREFIX):
            manifest = wiring.load_manifest_for_render(manifest_dir, sector)
            skill_pack = skill_packs[manifest.skill_pack]
            friendly_reason = _build_clarification_message(
                sector, skill_pack.required_fields, decision.reason
            )
            return jsonify({"status": _NEEDS_CLARIFICATION, "reason": friendly_reason}), 200

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

    @app.get("/pending")
    def get_pending():
        sector = request.args.get("sector")
        if sector is not None and sector not in _SECTORS:
            return jsonify({"error": f"unknown sector: {sector!r}"}), 400

        items = pending_store.list(sector=sector)
        return jsonify({"items": [_pending_item_to_json(item) for item in items]}), 200

    @app.post("/pending/<request_id>/accept")
    def accept_pending(request_id):
        item = pending_store.get(request_id)
        if item is None:
            return jsonify({"error": f"no pending request with id {request_id!r}"}), 404

        booking_request = BookingRequest(
            sector=item["sector"], fields=item["fields"], raw_text=item["text"]
        )
        skill_pack = skill_packs[item["skill_pack"]]
        try:
            slot = skill_pack.slot_rules(booking_request)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422

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
                timestamp=datetime.now(wiring.UAE_TZ),
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
                timestamp=datetime.now(wiring.UAE_TZ),
            )
        )
        pending_store.remove(request_id)

        return jsonify({"status": "REJECTED"}), 200

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

    @app.get("/config")
    def get_config():
        config = runtime_config.effective()
        return (
            jsonify(
                {
                    "base_url": config["base_url"],
                    "model": config["model"],
                    "api_key_set": bool(config["api_key"]),
                    "timeout": config["timeout"],
                }
            ),
            200,
        )

    @app.post("/config")
    def post_config():
        # Two-pass: validate every field in the body first, without
        # touching `runtime_config`, and only apply once everything has
        # validated successfully. This keeps a rejected request (e.g. a
        # valid `base_url` followed by an invalid `model`) from partially
        # applying -- a 400 response must mean nothing changed.
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        string_updates = {}
        for field_name in ("base_url", "model", "api_key"):
            if field_name in body:
                value = body[field_name]
                if not isinstance(value, str):
                    return jsonify({"error": f"'{field_name}' must be a string"}), 400
                string_updates[field_name] = value if value != "" else None

        timeout_update = None
        timeout_provided = "timeout" in body
        if timeout_provided:
            value = body["timeout"]
            if value is None:
                timeout_update = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                return jsonify({"error": "'timeout' must be a number or null"}), 400
            elif value <= 0:
                return jsonify({"error": "'timeout' must be a positive number"}), 400
            else:
                timeout_update = float(value)

        for field_name, value in string_updates.items():
            setattr(runtime_config, field_name, value)
        if timeout_provided:
            runtime_config.timeout = timeout_update

        config = runtime_config.effective()
        return (
            jsonify(
                {
                    "base_url": config["base_url"],
                    "model": config["model"],
                    "api_key_set": bool(config["api_key"]),
                    "timeout": config["timeout"],
                }
            ),
            200,
        )

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
        if "remove_practitioners" in body:
            names = body["remove_practitioners"]
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                return (
                    jsonify({"error": "'remove_practitioners' must be a list of names"}),
                    400,
                )
            for name in names:
                practitioners.pop(name, None)

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

        if not practitioners:
            return (
                jsonify({"error": "cannot remove the last practitioner -- at least one must remain"}),
                400,
            )

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
        if "remove_tables" in body:
            table_ids = body["remove_tables"]
            if not isinstance(table_ids, list) or not all(isinstance(t, str) for t in table_ids):
                return jsonify({"error": "'remove_tables' must be a list of table ids"}), 400
            for table_id in table_ids:
                tables.pop(table_id, None)

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

        if not tables:
            return (
                jsonify({"error": "cannot remove the last table -- at least one must remain"}),
                400,
            )

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

    return app


def run() -> None:
    """Console-script entry point (`eais-book-server`). Runs the dev server
    on 127.0.0.1:5000 with default manifest/audit locations -- adequate for
    the prototype/demo use this interface exists for; see the design spec's
    "Out of scope" section for what this deliberately does not add
    (HTTPS, auth, production WSGI server, etc.).
    """
    create_app().run()
