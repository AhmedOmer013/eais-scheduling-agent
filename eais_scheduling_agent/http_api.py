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
from pathlib import Path
from typing import Optional, Union

from flask import Flask, jsonify, render_template, request

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.gate import StandardApprovalGate
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

_CONFIRMED = "CONFIRMED"

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
) -> Flask:
    """Build a Flask app with a shared store/gate/audit for its whole lifetime.

    Args:
        manifest_dir: Directory holding one `<sector>.yaml` manifest per
            sector (default: the package's bundled manifests directory,
            same default `eais-book` uses).
        audit_file: JSON Lines audit file both `POST /bookings` appends
            to and `GET /audit` reads back (default: `audit.jsonl`,
            already git-ignored -- same default `eais-book` uses).
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
                LLMIntake(fallback=OfflineIntake(), client=client)
            )
        else:
            intake = wiring.CachingIntake(OfflineIntake())

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

        return jsonify({"status": "PENDING_APPROVAL", "reason": decision.reason}), 200

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

    return app


def run() -> None:
    """Console-script entry point (`eais-book-server`). Runs the dev server
    on 127.0.0.1:5000 with default manifest/audit locations -- adequate for
    the prototype/demo use this interface exists for; see the design spec's
    "Out of scope" section for what this deliberately does not add
    (HTTPS, auth, production WSGI server, etc.).
    """
    create_app().run()
