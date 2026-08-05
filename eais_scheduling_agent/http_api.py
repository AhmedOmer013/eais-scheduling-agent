"""Optional HTTP interface for the scheduling agent.

A thin Flask wrapper around the same `SchedulingAgentCore` the CLI (T14)
uses -- no new decision logic lives here; `core/` is untouched by this
module. See docs/superpowers/specs/2026-08-05-http-interface-design.md
for the full design rationale.

The one deliberate behavioral difference from the CLI: `create_app()`
builds its collaborators once, at app-creation time, and holds them for
the server's lifetime. The CLI builds a fresh `InMemoryBookingStore` per
process (per invocation), so two separate `eais-book` calls can never
conflict with each other; a long-running server is one continuous
process, so sharing one store here makes cross-request conflict
detection real for as long as the server runs (see
`tests/test_http_api.py::TestSharedStoreAcrossRequests`).

Two `SchedulingAgentCore` instances are built -- one offline-intake, one
LLM-intake -- because `SchedulingAgentCore.handle()` has no per-call way
to select intake mode; `POST /bookings`'s per-request `"llm"` flag picks
which core handles that request. Both cores are wired to the *same*
`skill_packs`, `gate`, `store`, and `audit` instances, so which core
handles a given request never affects shared-state consistency. Both
cores' intake is wrapped in `wiring.CachingIntake` at app-creation time
(not per-request) so the second `intake.parse()` call this module makes
to render a CONFIRMED message is always a cache hit against the exact
call `core.handle()` already made -- this cache lives for the server's
lifetime, which is an accepted, documented trade-off for a prototype
(unbounded by request volume, not by wall-clock time or request count),
not a correctness issue: the project's own `RestaurantSkillPack` static
table-assignment trade-off in `DESIGN.md` Section 3 is the same kind of
disclosed simplification.
"""

import json
from pathlib import Path
from typing import Union

from flask import Flask, jsonify, request

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.gate import StandardApprovalGate
from eais_scheduling_agent.core.orchestrator import (
    InvalidManifestError,
    SchedulingAgentCore,
    SectorDisabledError,
    UnknownSectorError,
    UnknownSkillPackError,
)
from eais_scheduling_agent.core.store import InMemoryBookingStore
from eais_scheduling_agent.intake.llm import LLMIntake
from eais_scheduling_agent.intake.offline import OfflineIntake

_CONFIRMED = "CONFIRMED"


def create_app(
    manifest_dir: Union[str, Path] = wiring.DEFAULT_MANIFEST_DIR,
    audit_file: Union[str, Path] = "audit.jsonl",
) -> Flask:
    """Build a Flask app with one shared core/store for its whole lifetime.

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

    offline_intake = wiring.CachingIntake(OfflineIntake())
    llm_intake = wiring.CachingIntake(LLMIntake(fallback=OfflineIntake()))

    offline_core = SchedulingAgentCore(
        manifest_dir=manifest_dir,
        skill_packs=skill_packs,
        intake=offline_intake,
        gate=gate,
        store=store,
        audit=audit,
    )
    llm_core = SchedulingAgentCore(
        manifest_dir=manifest_dir,
        skill_packs=skill_packs,
        intake=llm_intake,
        gate=gate,
        store=store,
        audit=audit,
    )

    @app.post("/bookings")
    def post_booking():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "sector" not in body or "text" not in body:
            return (
                jsonify({"error": "request body must be a JSON object with 'sector' and 'text'"}),
                400,
            )

        sector = body["sector"]
        text = body["text"]
        use_llm = bool(body.get("llm", False))
        core = llm_core if use_llm else offline_core
        intake = llm_intake if use_llm else offline_intake

        try:
            decision = core.handle(text, sector)
        except UnknownSectorError as exc:
            return jsonify({"error": str(exc)}), 404
        except SectorDisabledError as exc:
            return jsonify({"error": str(exc)}), 400
        except (InvalidManifestError, UnknownSkillPackError) as exc:
            return jsonify({"error": str(exc)}), 500

        if decision.status == _CONFIRMED:
            manifest = wiring.load_manifest_for_render(manifest_dir, sector)
            skill_pack = skill_packs[manifest.skill_pack]
            booking_request = intake.parse(text, sector)  # cache hit
            message = wiring.render_confirmation(skill_pack, booking_request)
            return jsonify({"status": "CONFIRMED", "message": message}), 200

        return jsonify({"status": "PENDING_APPROVAL", "reason": decision.reason}), 200

    @app.get("/audit")
    def get_audit():
        path = Path(audit_file)
        records = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return jsonify({"records": records}), 200

    return app
