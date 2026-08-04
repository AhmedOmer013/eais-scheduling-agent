"""Core data model types for the scheduling agent.

This module defines the three fundamental data types passed through all
components: BookingRequest, Decision, and AuditRecord.

These are pure data types with no sector-specific logic or orchestration.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class BookingRequest:
    """A booking request captured from unstructured input.

    Attributes:
        sector: Identifier tag (e.g. "clinic", "restaurant") attached by intake.
        fields: Structured fields extracted by intake (sector- and skill-pack-defined).
        raw_text: Original free-text input, kept for audit.
    """
    sector: str
    fields: dict
    raw_text: str


@dataclass(frozen=True)
class Decision:
    """A decision on whether to confirm or flag a booking for approval.

    Attributes:
        status: Either "CONFIRMED" or "PENDING_APPROVAL".
        reason: None/empty on CONFIRMED, specific human-readable reason on PENDING_APPROVAL.
    """
    status: str
    reason: Optional[str] = None

    def __post_init__(self):
        """Validate that status is one of the allowed values."""
        valid_statuses = {"CONFIRMED", "PENDING_APPROVAL"}
        if self.status not in valid_statuses:
            raise ValueError(
                f"Decision.status must be one of {valid_statuses}, got {self.status!r}"
            )


@dataclass(frozen=True)
class AuditRecord:
    """A single audit record for a processed booking request.

    Attributes:
        input: The original input (typically raw_text from BookingRequest).
        skill_pack: Identifier of the skill pack that processed this request.
        intent: Dict of extracted intent/structured data.
        rules_evaluated: List of rules evaluated during processing.
        decision: The decision status (e.g. "CONFIRMED" or "PENDING_APPROVAL").
        approval_status: Current approval status (e.g. "pending", "approved", "rejected").
        timestamp: When the record was created.
    """
    input: str
    skill_pack: str
    intent: dict
    rules_evaluated: list
    decision: str
    approval_status: str
    timestamp: datetime
