"""Tests for the optional HTTP interface (eais_scheduling_agent.http_api).

Uses Flask's built-in `test_client()` exclusively -- Flask itself never
opens a real socket (`test_client()` is in-process), matching the
project's existing "no network access in tests" discipline (see
README.md's "Run tests" section). The one exception is the `"llm": true`
path (`TestLLMFlagFallsBackCleanly` below), which exercises the real
`LLMIntake` -> `OpenAICompatibleHTTPClient` -> `urllib.request.urlopen(...)` chain
and does attempt (and gracefully fall back from) a real loopback
connection to `localhost:11434` -- the same situation
`tests/test_cli.py`'s equivalent test documents.

Flask is an optional dependency (the `http` extra); this whole module is
skipped cleanly, not a collection error, when it isn't installed.
"""

import pytest

flask = pytest.importorskip("flask")

from eais_scheduling_agent.http_api import create_app


@pytest.fixture
def client(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    app = create_app(audit_file=str(audit_path))
    app.testing = True
    return app.test_client()


class TestClinicBookingConfirmed:
    def test_confirms_and_returns_message(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "CONFIRMED"
        assert "John Doe" in body["message"]
        assert "Dr. A" in body["message"]


class TestRestaurantBookingConfirmed:
    def test_confirms_and_returns_message(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm, customer Jane Smith"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "CONFIRMED"
        assert "Jane Smith" in body["message"]


class TestPendingApproval:
    def test_outside_working_hours_returns_reason(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 6am, patient John Doe"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "PENDING_APPROVAL"
        assert "outside working hours" in body["reason"]


class TestUnknownSector:
    def test_returns_404(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "veterinary", "text": "some request text"},
        )
        assert response.status_code == 404
        assert "veterinary" in response.get_json()["error"]


class TestMalformedBody:
    def test_missing_text_returns_400(self, client):
        response = client.post("/bookings", json={"sector": "clinic"})
        assert response.status_code == 400

    def test_non_json_body_returns_400(self, client):
        response = client.post("/bookings", data="not json", content_type="text/plain")
        assert response.status_code == 400


class TestLLMFlagFallsBackCleanly:
    """No LLM backend reachable in this environment or CI
    (same situation `tests/test_cli.py::TestLLMFlagFallsBackCleanly`
    documents) -- `"llm": true` still confirms via automatic fallback.
    """

    def test_llm_true_falls_back_and_still_confirms(self, client, monkeypatch):
        # Clear any ambient EAIS_LLM_* config so this test always targets
        # the unreachable-by-default localhost endpoint, regardless of
        # what a developer running this suite locally has exported to
        # point "llm": true at their own real backend (exactly what this
        # branch exists to let them do).
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe", "llm": True},
        )
        assert response.status_code == 200
        assert response.get_json()["status"] == "CONFIRMED"


class TestAuditEndpoint:
    def test_returns_empty_list_before_any_booking(self, client):
        response = client.get("/audit")
        assert response.status_code == 200
        assert response.get_json()["records"] == []

    def test_returns_one_record_per_request(self, client):
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
        assert records[0]["decision"] == "CONFIRMED"
        assert records[1]["decision"] == "CONFIRMED"


class TestSharedStoreAcrossRequests:
    """The one test that exercises the shared-store design decision
    directly: the CLI cannot show this (see tests/test_cli.py -- each
    invocation is a fresh process with a fresh store), but two requests
    through the *same* running server can genuinely conflict.
    """

    def test_second_booking_for_same_slot_conflicts(self, client):
        first = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        assert first.get_json()["status"] == "CONFIRMED"

        second = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient Second Patient"},
        )
        body = second.get_json()
        assert body["status"] == "PENDING_APPROVAL"
        assert "conflicts with an existing booking" in body["reason"]
