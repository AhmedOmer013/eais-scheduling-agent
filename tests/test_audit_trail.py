"""Tests for JSON Lines audit trail implementation.

Covers all requirements from the task brief:
- Single record writes exactly one line
- Valid JSON round-tripping
- Multiple records (CONFIRMED and PENDING_APPROVAL) with schema validation
- Directory auto-creation or error handling
- Append mode (no truncation on restart)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eais_scheduling_agent.core.audit import JsonLinesAuditTrail
from eais_scheduling_agent.core.models import AuditRecord


@pytest.fixture
def audit_record_confirmed():
    """A CONFIRMED decision audit record for testing."""
    return AuditRecord(
        input="Schedule appointment for John on 2024-08-05",
        skill_pack="clinic_scheduler",
        intent={"patient": "John", "date": "2024-08-05"},
        rules_evaluated=[
            "required_fields: ok",
            "skill_pack_validation: ok",
            "conflict_check: none",
        ],
        decision="CONFIRMED",
        approval_status="not_required",
        timestamp=datetime(2024, 8, 5, 10, 30, 45, 123456, tzinfo=timezone.utc),
    )


@pytest.fixture
def audit_record_pending():
    """A PENDING_APPROVAL decision audit record for testing."""
    return AuditRecord(
        input="Schedule appointment on Christmas",
        skill_pack="clinic_scheduler",
        intent={"date": "2024-12-25"},
        rules_evaluated=[
            "required_fields: ok",
            "skill_pack_validation: violations: holiday booking",
            "conflict_check: skipped (rules not satisfied)",
        ],
        decision="PENDING_APPROVAL",
        approval_status="pending",
        timestamp=datetime(2024, 8, 6, 14, 15, 30, 456789, tzinfo=timezone.utc),
    )


class TestAuditTrailSingleRecord:
    """Test appending a single audit record."""

    def test_single_record_writes_exactly_one_line(self, tmp_path, audit_record_confirmed):
        """Appending one record writes exactly one line to the file."""
        audit_file = tmp_path / "audit.jsonl"
        trail = JsonLinesAuditTrail(audit_file)

        trail.append(audit_record_confirmed)

        # Read the file and verify line count
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
        assert lines[0], "Line should not be empty"


class TestAuditTrailJsonRoundTrip:
    """Test JSON serialization and round-tripping."""

    def test_written_json_is_valid_and_roundtrips(
        self, tmp_path, audit_record_confirmed
    ):
        """Written line is valid JSON and round-trips correctly."""
        audit_file = tmp_path / "audit.jsonl"
        trail = JsonLinesAuditTrail(audit_file)

        trail.append(audit_record_confirmed)

        # Read and parse the line
        line = audit_file.read_text(encoding="utf-8").strip()
        parsed = json.loads(line)

        # Verify all fields match (accounting for datetime->ISO conversion)
        assert parsed["input"] == audit_record_confirmed.input
        assert parsed["skill_pack"] == audit_record_confirmed.skill_pack
        assert parsed["intent"] == audit_record_confirmed.intent
        assert parsed["rules_evaluated"] == audit_record_confirmed.rules_evaluated
        assert parsed["decision"] == audit_record_confirmed.decision
        assert parsed["approval_status"] == audit_record_confirmed.approval_status
        # Verify timestamp is ISO 8601 string and matches the original
        assert (
            parsed["timestamp"]
            == audit_record_confirmed.timestamp.isoformat()
        )


class TestAuditTrailMultipleRecords:
    """Test appending multiple records with different decision statuses."""

    def test_multiple_records_with_different_statuses(
        self, tmp_path, audit_record_confirmed, audit_record_pending
    ):
        """Multiple records (CONFIRMED and PENDING_APPROVAL) write one line each, in order."""
        audit_file = tmp_path / "audit.jsonl"
        trail = JsonLinesAuditTrail(audit_file)

        # Append both records
        trail.append(audit_record_confirmed)
        trail.append(audit_record_pending)

        # Read and verify
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

        # Verify order and content
        parsed_1 = json.loads(lines[0])
        parsed_2 = json.loads(lines[1])

        assert parsed_1["decision"] == "CONFIRMED"
        assert parsed_1["approval_status"] == "not_required"

        assert parsed_2["decision"] == "PENDING_APPROVAL"
        assert parsed_2["approval_status"] == "pending"

        # Verify the timestamp order is preserved
        assert lines[0] != lines[1], "Records should be different"


class TestAuditTrailSchema:
    """Test schema validation of written records."""

    def test_schema_has_exact_keys_and_types(
        self, tmp_path, audit_record_confirmed
    ):
        """Verify schema: exact keys and correct JSON types."""
        audit_file = tmp_path / "audit.jsonl"
        trail = JsonLinesAuditTrail(audit_file)

        trail.append(audit_record_confirmed)

        line = audit_file.read_text(encoding="utf-8").strip()
        parsed = json.loads(line)

        # Verify exact keys
        expected_keys = {
            "input",
            "skill_pack",
            "intent",
            "rules_evaluated",
            "decision",
            "approval_status",
            "timestamp",
        }
        assert set(parsed.keys()) == expected_keys

        # Verify JSON types
        assert isinstance(parsed["input"], str)
        assert isinstance(parsed["skill_pack"], str)
        assert isinstance(parsed["intent"], dict)
        assert isinstance(parsed["rules_evaluated"], list)
        assert len(parsed["rules_evaluated"]) == 3
        assert all(isinstance(rule, str) for rule in parsed["rules_evaluated"])
        assert isinstance(parsed["decision"], str)
        assert isinstance(parsed["approval_status"], str)
        assert isinstance(parsed["timestamp"], str)


class TestAuditTrailDirectoryHandling:
    """Test directory creation and error handling."""

    def test_auto_creates_parent_directory(self, tmp_path, audit_record_confirmed):
        """Parent directory is auto-created if it doesn't exist."""
        nested_path = tmp_path / "deep" / "nested" / "dir" / "audit.jsonl"
        trail = JsonLinesAuditTrail(nested_path)

        # Should not raise; directory should be created
        trail.append(audit_record_confirmed)

        assert nested_path.exists(), "Audit file should exist after append"
        assert nested_path.parent.exists(), "Parent directory should be created"
        lines = nested_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1


class TestAuditTrailAppendMode:
    """Test that multiple instances append without truncation."""

    def test_two_instances_append_without_truncation(
        self, tmp_path, audit_record_confirmed, audit_record_pending
    ):
        """Two instances writing to same path append; no truncation."""
        audit_file = tmp_path / "audit.jsonl"

        # First instance appends one record
        trail1 = JsonLinesAuditTrail(audit_file)
        trail1.append(audit_record_confirmed)

        # Second instance appends another record (simulating restart)
        trail2 = JsonLinesAuditTrail(audit_file)
        trail2.append(audit_record_pending)

        # Verify both records are present
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

        # Verify both decisions are present
        parsed_1 = json.loads(lines[0])
        parsed_2 = json.loads(lines[1])
        assert parsed_1["decision"] == "CONFIRMED"
        assert parsed_2["decision"] == "PENDING_APPROVAL"
