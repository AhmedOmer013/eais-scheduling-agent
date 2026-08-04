"""Tests for sector manifest loader."""

import pytest
from pathlib import Path

from eais_scheduling_agent.manifests.manifest import (
    SectorManifest,
    ManifestValidationError,
)


# Get the fixtures directory path
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "manifests"


class TestValidManifests:
    """Test loading valid manifests."""

    def test_load_valid_yaml_manifest(self):
        """A valid YAML manifest loads correctly into a SectorManifest."""
        manifest_path = FIXTURES_DIR / "valid.yaml"
        manifest = SectorManifest.load(str(manifest_path))

        assert manifest.sector == "clinic"
        assert manifest.enabled is True
        assert manifest.skill_pack == "clinic_v1"
        assert manifest.approval_required_for == [
            "outside_working_hours",
            "double_booking",
            "missing_required_field",
        ]

    def test_load_valid_json_manifest(self):
        """A valid JSON manifest loads correctly into a SectorManifest."""
        manifest_path = FIXTURES_DIR / "valid.json"
        manifest = SectorManifest.load(str(manifest_path))

        assert manifest.sector == "restaurant"
        assert manifest.enabled is True
        assert manifest.skill_pack == "restaurant_v1"
        assert manifest.approval_required_for == [
            "peak_hours",
            "large_party",
            "special_requests",
        ]


class TestMissingFields:
    """Test handling of missing required fields."""

    def test_missing_skill_pack_field(self):
        """A manifest missing skill_pack raises ManifestValidationError."""
        manifest_path = FIXTURES_DIR / "missing_field.yaml"
        with pytest.raises(ManifestValidationError) as exc_info:
            SectorManifest.load(str(manifest_path))

        assert "skill_pack" in str(exc_info.value)
        assert "Missing required field" in str(exc_info.value)


class TestWrongTypes:
    """Test handling of wrong-typed fields."""

    def test_wrong_type_enabled_field(self):
        """A manifest with enabled as string raises ManifestValidationError."""
        manifest_path = FIXTURES_DIR / "wrong_type.yaml"
        with pytest.raises(ManifestValidationError) as exc_info:
            SectorManifest.load(str(manifest_path))

        assert "enabled" in str(exc_info.value)
        assert "must be a bool" in str(exc_info.value)


class TestNonexistentFile:
    """Test handling of nonexistent file paths."""

    def test_nonexistent_file_raises_error(self):
        """A nonexistent file path raises a clear error."""
        nonexistent_path = FIXTURES_DIR / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            SectorManifest.load(str(nonexistent_path))


class TestFrozenDataclass:
    """Test that SectorManifest is frozen."""

    def test_sector_manifest_is_frozen(self):
        """SectorManifest instances are immutable."""
        manifest_path = FIXTURES_DIR / "valid.yaml"
        manifest = SectorManifest.load(str(manifest_path))

        with pytest.raises(Exception):  # FrozenInstanceError for frozen dataclass
            manifest.sector = "changed"
