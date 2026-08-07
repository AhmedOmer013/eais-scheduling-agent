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

from datetime import datetime

import pytest

flask = pytest.importorskip("flask")

from eais_scheduling_agent import wiring
from eais_scheduling_agent.http_api import create_app


@pytest.fixture
def client(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    pending_path = tmp_path / "pending_requests.json"
    app = create_app(audit_file=str(audit_path), pending_file=str(pending_path))
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


class TestAuditEndpointToleratesCorruptLine:
    """A killed-mid-write server process could leave the last line of an
    audit file truncated. GET /audit should skip just that line, not
    500 for the whole dashboard -- see _read_audit_records.
    """

    def test_skips_corrupt_line_instead_of_500ing(self, client, tmp_path):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )

        clinic_audit_path = tmp_path / "audit.clinic.jsonl"
        with clinic_audit_path.open("a", encoding="utf-8") as f:
            f.write('{"truncated": "line"\n')  # missing closing brace -- invalid JSON

        response = client.get("/audit?sector=clinic")

        assert response.status_code == 200
        records = response.get_json()["records"]
        assert len(records) == 1
        assert records[0]["decision"] == "CONFIRMED"

    def test_merged_view_also_tolerates_a_corrupt_line(self, client, tmp_path):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )
        client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm, customer Jane Smith"},
        )

        restaurant_audit_path = tmp_path / "audit.restaurant.jsonl"
        with restaurant_audit_path.open("a", encoding="utf-8") as f:
            f.write("not even close to json\n")

        response = client.get("/audit")

        assert response.status_code == 200
        records = response.get_json()["records"]
        assert len(records) == 2


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


class TestGetConfig:
    def test_returns_defaults_when_nothing_configured(self, client, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        response = client.get("/config")

        assert response.status_code == 200
        assert response.get_json() == {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3.2",
            "api_key_set": False,
            "timeout": 60.0,
        }


class TestPostConfig:
    def test_sets_base_url_override_and_get_reflects_it(self, client):
        response = client.post("/config", json={"base_url": "http://100.64.0.5:8000/v1"})
        assert response.status_code == 200
        assert response.get_json()["base_url"] == "http://100.64.0.5:8000/v1"

        follow_up = client.get("/config")
        assert follow_up.get_json()["base_url"] == "http://100.64.0.5:8000/v1"

    def test_sets_api_key_and_never_returns_raw_value(self, client):
        response = client.post("/config", json={"api_key": "secret-key"})

        assert response.status_code == 200
        body = response.get_json()
        assert body["api_key_set"] is True
        assert "api_key" not in body

    def test_empty_string_clears_a_string_override(self, client, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        client.post("/config", json={"model": "custom-model"})

        response = client.post("/config", json={"model": ""})

        assert response.status_code == 200
        assert response.get_json()["model"] == "llama3.2"

    def test_absent_field_leaves_existing_override_untouched(self, client):
        client.post("/config", json={"model": "custom-model"})

        response = client.post("/config", json={"base_url": "http://example.invalid/v1"})

        assert response.status_code == 200
        assert response.get_json()["model"] == "custom-model"

    def test_sets_numeric_timeout_override(self, client):
        response = client.post("/config", json={"timeout": 120})

        assert response.status_code == 200
        assert response.get_json()["timeout"] == 120.0

    def test_null_timeout_clears_override(self, client, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)
        client.post("/config", json={"timeout": 90})

        response = client.post("/config", json={"timeout": None})

        assert response.status_code == 200
        assert response.get_json()["timeout"] == 60.0

    def test_non_numeric_timeout_returns_400(self, client):
        response = client.post("/config", json={"timeout": "soon"})

        assert response.status_code == 400

    def test_boolean_timeout_returns_400(self, client):
        # bool is a subclass of int in Python -- must be explicitly
        # rejected, same precedent as intake/llm.py's _validate_party_size.
        response = client.post("/config", json={"timeout": True})

        assert response.status_code == 400

    def test_zero_timeout_returns_400(self, client):
        response = client.post("/config", json={"timeout": 0})

        assert response.status_code == 400

    def test_negative_timeout_returns_400(self, client):
        response = client.post("/config", json={"timeout": -5})

        assert response.status_code == 400

    def test_non_string_base_url_returns_400(self, client):
        response = client.post("/config", json={"base_url": 12345})

        assert response.status_code == 400

    def test_malformed_body_returns_400(self, client):
        response = client.post("/config", data="not json", content_type="text/plain")

        assert response.status_code == 400

    def test_rejected_request_does_not_partially_apply(self, client, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        response = client.post(
            "/config", json={"base_url": "http://evil.invalid/v1", "model": 12345}
        )

        assert response.status_code == 400

        follow_up = client.get("/config")
        assert follow_up.get_json()["base_url"] == "http://localhost:11434/v1"


class TestDashboardPage:
    def test_returns_200_with_expected_sections(self, client):
        response = client.get("/")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'id="booking-form"' in html
        assert 'id="audit-body-clinic"' in html
        assert 'id="audit-body-restaurant"' in html
        assert 'id="config-form"' in html


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
        app = create_app(
            audit_file=str(audit_path), pending_file=str(tmp_path / "pending_requests.json")
        )
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


class TestConfigOverrideReachesBookingRequest:
    def test_post_config_changes_the_client_used_by_llm_bookings(self, client, monkeypatch):
        captured = {}

        class _RecordingClient:
            def __init__(self, base_url, model, api_key=None, timeout=60.0):
                captured["base_url"] = base_url
                captured["model"] = model
                captured["timeout"] = timeout

            def __call__(self, prompt):
                raise ConnectionError("recording stub never actually calls out")

        monkeypatch.setattr(
            "eais_scheduling_agent.http_api.OpenAICompatibleHTTPClient", _RecordingClient
        )

        client.post(
            "/config",
            json={"base_url": "http://example.invalid/v1", "model": "custom-test-model"},
        )
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe", "llm": True},
        )

        assert response.status_code == 200
        assert captured["base_url"] == "http://example.invalid/v1"
        assert captured["model"] == "custom-test-model"


class TestNeedsClarification:
    def test_missing_required_field_returns_needs_clarification(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "book me in with the doctor tomorrow"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "NEEDS_CLARIFICATION"
        assert "missing:" in body["reason"]

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


class TestFriendlyClarificationMessage:
    """The raw core/gate.py reason ("missing required field(s): patient_name")
    names fields by their internal key, not a name a non-technical user
    would recognize. These tests verify NEEDS_CLARIFICATION translates
    that into plain language: what this sector's bookings need, and
    exactly which of those is actually missing -- not the full required
    set redundantly, and not raw field keys like "patient_name".
    """

    def test_clinic_lists_only_the_actually_missing_field(self, client):
        response = client.post(
            "/bookings", json={"sector": "clinic", "text": "Dr. A today at 10am"}
        )
        body = response.get_json()
        assert body["status"] == "NEEDS_CLARIFICATION"
        assert "doctor's name" in body["reason"]
        assert "timing" in body["reason"]
        assert "patient's name" in body["reason"]

        missing_clause = body["reason"].split("missing:")[1]
        assert "patient's name" in missing_clause
        assert "doctor's name" not in missing_clause
        assert "timing" not in missing_clause

    def test_restaurant_lists_only_the_actually_missing_field(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "restaurant", "text": "table for 4 today at 6pm"},
        )
        body = response.get_json()
        assert body["status"] == "NEEDS_CLARIFICATION"
        assert "party size" in body["reason"]
        assert "name on the booking" in body["reason"]
        assert "timing" in body["reason"]

        missing_clause = body["reason"].split("missing:")[1]
        assert "name on the booking" in missing_clause
        assert "party size" not in missing_clause

    def test_clinic_all_fields_missing_lists_all_three(self, client):
        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "book me in with the doctor tomorrow"},
        )
        body = response.get_json()

        missing_clause = body["reason"].split("missing:")[1]
        assert "doctor's name" in missing_clause
        assert "timing" in missing_clause
        assert "patient's name" in missing_clause


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
            json={"sector": "restaurant", "text": "table for 99 today at 6pm, customer Jane Smith"},
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


class TestAcceptPendingRequest:
    def _queue_one(self, client, text="Dr. A today at 6am, patient John Doe"):
        client.post("/bookings", json={"sector": "clinic", "text": text})
        # Match by `text` rather than indexing items[0]: some tests in this
        # class queue two items in the same run, and PendingRequestStore.list()
        # returns items in insertion order, so items[0] would keep resolving
        # to the *first* queued item instead of the one just queued here.
        items = client.get("/pending").get_json()["items"]
        return next(item["id"] for item in items if item["text"] == text)

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
        first_id = self._queue_one(client, text="Dr. A today at 6am, patient John Doe")
        second_id = self._queue_one(client, text="Dr. A today at 6am, patient Jane Roe")
        client.post(f"/pending/{first_id}/accept")  # takes the slot

        response = client.post(f"/pending/{second_id}/accept")

        assert response.status_code == 409
        assert client.get(f"/pending").get_json()["items"][0]["id"] == second_id

    def test_accept_unknown_practitioner_returns_422_and_stays_pending(self, client):
        request_id = self._queue_one(client, text="Dr. Chen today at 10am, patient John Doe")

        response = client.post(f"/pending/{request_id}/accept")

        assert response.status_code == 422
        items = client.get("/pending").get_json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == request_id


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


class TestClinicConfigDeletion:
    def test_post_removes_a_practitioner(self, client):
        response = client.post("/config/clinic", json={"remove_practitioners": ["Dr. B"]})
        assert response.status_code == 200
        assert response.get_json()["practitioners"] == {"Dr. A": 30}

    def test_removed_practitioner_is_no_longer_bookable(self, client):
        client.post("/config/clinic", json={"remove_practitioners": ["Dr. B"]})

        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. B today at 10am, patient John Doe"},
        )

        assert response.get_json()["status"] == "PENDING_APPROVAL"
        assert "unknown practitioner" in response.get_json()["reason"]

    def test_remove_and_re_add_in_the_same_request_keeps_it_with_the_new_value(self, client):
        response = client.post(
            "/config/clinic",
            json={"remove_practitioners": ["Dr. A"], "practitioners": {"Dr. A": 99}},
        )
        assert response.status_code == 200
        assert response.get_json()["practitioners"]["Dr. A"] == 99

    def test_removing_an_unknown_practitioner_is_a_no_op(self, client):
        response = client.post("/config/clinic", json={"remove_practitioners": ["Dr. Z"]})
        assert response.status_code == 200
        assert response.get_json()["practitioners"] == {"Dr. A": 30, "Dr. B": 20}

    def test_rejects_removing_the_last_practitioner(self, client):
        response = client.post(
            "/config/clinic", json={"remove_practitioners": ["Dr. A", "Dr. B"]}
        )
        assert response.status_code == 400
        # Nothing was applied -- config unchanged.
        assert client.get("/config/clinic").get_json()["practitioners"] == {
            "Dr. A": 30,
            "Dr. B": 20,
        }

    def test_rejects_non_list_remove_practitioners(self, client):
        response = client.post("/config/clinic", json={"remove_practitioners": "Dr. A"})
        assert response.status_code == 400

    def test_rejects_non_string_items_in_remove_practitioners(self, client):
        response = client.post("/config/clinic", json={"remove_practitioners": [123]})
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


class TestRestaurantConfigDeletion:
    def test_post_removes_a_table(self, client):
        response = client.post("/config/restaurant", json={"remove_tables": ["T5"]})
        assert response.status_code == 200
        assert "T5" not in response.get_json()["tables"]

    def test_removed_table_is_no_longer_assignable(self, client):
        # T5 (capacity 8) is the only table big enough for a party of 8 --
        # removing it means that party size can no longer be seated at all.
        client.post("/config/restaurant", json={"remove_tables": ["T5"]})

        response = client.post(
            "/bookings",
            json={
                "sector": "restaurant",
                "text": "table for 8 today at 6pm, customer Jane Smith",
            },
        )

        assert response.get_json()["status"] == "PENDING_APPROVAL"
        assert "exceeds largest table capacity" in response.get_json()["reason"]

    def test_remove_and_re_add_in_the_same_request_keeps_it_with_the_new_value(self, client):
        response = client.post(
            "/config/restaurant",
            json={"remove_tables": ["T1"], "tables": {"T1": 99}},
        )
        assert response.status_code == 200
        assert response.get_json()["tables"]["T1"] == 99

    def test_removing_an_unknown_table_is_a_no_op(self, client):
        response = client.post("/config/restaurant", json={"remove_tables": ["T9"]})
        assert response.status_code == 200
        assert response.get_json()["tables"] == {"T1": 2, "T2": 2, "T3": 4, "T4": 6, "T5": 8}

    def test_rejects_removing_every_table(self, client):
        response = client.post(
            "/config/restaurant",
            json={"remove_tables": ["T1", "T2", "T3", "T4", "T5"]},
        )
        assert response.status_code == 400
        assert client.get("/config/restaurant").get_json()["tables"] == {
            "T1": 2,
            "T2": 2,
            "T3": 4,
            "T4": 6,
            "T5": 8,
        }

    def test_rejects_non_list_remove_tables(self, client):
        response = client.post("/config/restaurant", json={"remove_tables": "T1"})
        assert response.status_code == 400


class TestUaeTimezone:
    """The whole web app (dashboard + HTTP API) resolves relative dates
    and records timestamps in UAE wall-clock time (wiring.uae_now / .UAE_TZ)
    regardless of this machine's actual system clock timezone -- cli.py
    and core/ are untouched (same brief-scope boundary as every other
    extension), so this is scoped entirely to what http_api.py itself
    constructs or displays.
    """

    def test_booking_date_resolution_uses_injected_uae_now(self, client, monkeypatch):
        monkeypatch.setattr(wiring, "uae_now", lambda: datetime(2027, 1, 1, 10, 0, 0))

        response = client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )

        assert response.get_json()["status"] == "CONFIRMED"
        assert "2027-01-01 10:00:00" in response.get_json()["message"]

    def test_llm_flag_also_uses_injected_uae_now_for_its_offline_fallback(self, client, monkeypatch):
        # No LLM server reachable in this test environment -- LLMIntake
        # falls back to OfflineIntake, which must be the *same*
        # UAE-clocked instance, not a bare default.
        monkeypatch.setattr(wiring, "uae_now", lambda: datetime(2027, 1, 1, 10, 0, 0))

        response = client.post(
            "/bookings",
            json={
                "sector": "clinic",
                "text": "Dr. A today at 10am, patient John Doe",
                "llm": True,
            },
        )

        assert response.get_json()["status"] == "CONFIRMED"
        assert "2027-01-01 10:00:00" in response.get_json()["message"]

    def test_accept_pending_writes_a_uae_offset_timestamp(self, client):
        # Known practitioner, outside working hours -- slot_rules() can
        # compute a slot, so accept succeeds (unlike an unknown
        # practitioner, which 422s -- see TestAcceptPendingRequest).
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 6am, patient John Doe"},
        )
        request_id = client.get("/pending").get_json()["items"][0]["id"]

        client.post(f"/pending/{request_id}/accept")

        records = client.get("/audit?sector=clinic").get_json()["records"]
        approved = [r for r in records if r["approval_status"] == "approved"]
        assert approved[0]["timestamp"].endswith("+04:00")

    def test_reject_pending_writes_a_uae_offset_timestamp(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. Chen today at 10am, patient John Doe"},
        )
        request_id = client.get("/pending").get_json()["items"][0]["id"]

        client.post(f"/pending/{request_id}/reject")

        records = client.get("/audit?sector=clinic").get_json()["records"]
        rejected = [r for r in records if r["approval_status"] == "rejected"]
        assert rejected[0]["timestamp"].endswith("+04:00")

    def test_get_audit_converts_core_written_utc_timestamps_to_uae_for_display(self, client):
        # This record's timestamp is written by core/orchestrator.py as
        # datetime.now(timezone.utc) -- untouched, still UTC internally.
        # GET /audit must still show it in UAE time to the caller.
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )

        records = client.get("/audit?sector=clinic").get_json()["records"]

        assert records[0]["timestamp"].endswith("+04:00")

    def test_get_audit_merged_view_also_converts_to_uae(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. A today at 10am, patient John Doe"},
        )

        records = client.get("/audit").get_json()["records"]

        assert records[0]["timestamp"].endswith("+04:00")

    def test_get_pending_converts_created_at_to_uae(self, client):
        client.post(
            "/bookings",
            json={"sector": "clinic", "text": "Dr. Chen today at 10am, patient John Doe"},
        )

        items = client.get("/pending").get_json()["items"]

        assert items[0]["created_at"].endswith("+04:00")
