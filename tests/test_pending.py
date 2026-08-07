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
