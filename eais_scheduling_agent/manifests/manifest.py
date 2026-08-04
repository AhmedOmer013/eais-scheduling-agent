"""Sector manifest loader and validator."""

from dataclasses import dataclass
from pathlib import Path
from typing import List
import json

import yaml


class ManifestValidationError(Exception):
    """Raised when a manifest file is malformed or invalid."""
    pass


@dataclass(frozen=True)
class SectorManifest:
    """A sector manifest declaring a sector's configuration.

    Attributes:
        sector: Identifier tag (e.g. "clinic", "restaurant") for this sector.
        enabled: Whether this sector's agent is active.
        skill_pack: Identifier of the skill pack implementation to use for this sector.
        approval_required_for: List of rule-category strings that force PENDING_APPROVAL.
    """
    sector: str
    enabled: bool
    skill_pack: str
    approval_required_for: List[str]

    @classmethod
    def load(cls, path: str) -> "SectorManifest":
        """Load and validate a sector manifest from a YAML or JSON file.

        Args:
            path: Path to the manifest file (.yaml, .yml, or .json).

        Returns:
            A validated SectorManifest object.

        Raises:
            FileNotFoundError: If the file does not exist.
            ManifestValidationError: If the manifest is malformed or missing/wrong-typed fields.
        """
        file_path = Path(path)

        # Check if file exists
        if not file_path.exists():
            raise FileNotFoundError(f"Manifest file not found: {path}")

        # Read file based on extension
        suffix = file_path.suffix.lower()
        try:
            if suffix in {".yaml", ".yml"}:
                with open(file_path, "r") as f:
                    data = yaml.safe_load(f)
            elif suffix == ".json":
                with open(file_path, "r") as f:
                    data = json.load(f)
            else:
                raise ManifestValidationError(
                    f"Unsupported manifest file extension: {suffix}. "
                    f"Must be .yaml, .yml, or .json"
                )
        except (yaml.YAMLError, json.JSONDecodeError) as e:
            raise ManifestValidationError(
                f"Failed to parse manifest file {path}: {e}"
            ) from e
        except Exception as e:
            raise ManifestValidationError(
                f"Error reading manifest file {path}: {e}"
            ) from e

        if not isinstance(data, dict):
            raise ManifestValidationError(
                "Manifest must be a YAML/JSON object at the top level"
            )

        # Validate and extract each field
        try:
            sector = data.get("sector")
            if sector is None:
                raise ManifestValidationError("Missing required field: sector")
            if not isinstance(sector, str):
                raise ManifestValidationError(
                    f"Field 'sector' must be a string, got {type(sector).__name__}"
                )

            enabled = data.get("enabled")
            if enabled is None:
                raise ManifestValidationError("Missing required field: enabled")
            if not isinstance(enabled, bool):
                raise ManifestValidationError(
                    f"Field 'enabled' must be a bool, got {type(enabled).__name__}"
                )

            skill_pack = data.get("skill_pack")
            if skill_pack is None:
                raise ManifestValidationError("Missing required field: skill_pack")
            if not isinstance(skill_pack, str):
                raise ManifestValidationError(
                    f"Field 'skill_pack' must be a string, got {type(skill_pack).__name__}"
                )

            approval_required_for = data.get("approval_required_for")
            if approval_required_for is None:
                raise ManifestValidationError("Missing required field: approval_required_for")
            if not isinstance(approval_required_for, list):
                raise ManifestValidationError(
                    f"Field 'approval_required_for' must be a list, "
                    f"got {type(approval_required_for).__name__}"
                )
            if not all(isinstance(item, str) for item in approval_required_for):
                raise ManifestValidationError(
                    "Field 'approval_required_for' must be a list of strings"
                )

            return cls(
                sector=sector,
                enabled=enabled,
                skill_pack=skill_pack,
                approval_required_for=approval_required_for,
            )
        except ManifestValidationError:
            raise
        except Exception as e:
            raise ManifestValidationError(
                f"Unexpected error validating manifest: {e}"
            ) from e
