"""Optional HTTP interface for the scheduling agent.

A thin Flask wrapper around the same `SchedulingAgentCore` the CLI (T14)
uses -- no new decision logic lives here; `core/` is untouched by this
module. See docs/superpowers/specs/2026-08-05-http-interface-design.md
for the full design rationale.

The one deliberate behavioral difference from the CLI: `create_app()`
builds `store`, `gate`, `audit`, and `skill_packs` once, at app-creation
time, and holds them for the server's lifetime. The CLI builds a fresh
`InMemoryBookingStore` per process (per invocation), so two separate
`eais-book` calls can never conflict with each other; a long-running
server is one continuous process, so sharing one store here makes
cross-request conflict detection real for as long as the server runs
(see `tests/test_http_api.py::TestSharedStoreAcrossRequests`). That
reasoning assumes single-request-at-a-time handling: the dev server
(`run()` below) runs threaded by default, and `InMemoryBookingStore` has
no internal locking, so two truly concurrent requests are not guaranteed
to serialize correctly (a check-then-act race is possible) -- a known,
documented limitation of this prototype, not something this module
guards against with a mutex.

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
    audit = JsonLinesAuditTrail(path=audit_file)
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
            audit=audit,
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
        # Reads the whole audit file from disk on every call -- no
        # pagination, no auth. `audit_file` is shared with the CLI's
        # default output file and persists across server restarts, so
        # this can surface records from previous server runs and from
        # separate `eais-book` CLI invocations, even though the
        # in-memory `store` above resets on every restart -- i.e. this
        # can list CONFIRMED bookings `store` itself has no memory of.
        path = Path(audit_file)
        records = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
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
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400

        for field_name in ("base_url", "model", "api_key"):
            if field_name in body:
                value = body[field_name]
                if not isinstance(value, str):
                    return jsonify({"error": f"'{field_name}' must be a string"}), 400
                setattr(runtime_config, field_name, value if value != "" else None)

        if "timeout" in body:
            value = body["timeout"]
            if value is None:
                runtime_config.timeout = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                return jsonify({"error": "'timeout' must be a number or null"}), 400
            else:
                runtime_config.timeout = float(value)

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
