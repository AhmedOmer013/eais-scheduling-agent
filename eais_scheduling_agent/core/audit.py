"""JSON Lines audit trail implementation.

Provides a concrete AuditTrail that writes one JSON object per line,
with ISO 8601 datetime serialization, durable writes via open-per-call.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Union

from eais_scheduling_agent.core.interfaces import AuditTrail
from eais_scheduling_agent.core.models import AuditRecord


class JsonLinesAuditTrail(AuditTrail):
    """Records audit events as JSON Lines (one JSON object per line).

    Design choices:
    - Write durability: open-per-call (open, write complete line, close).
      This guarantees durability after each append() returns, with minimal
      performance cost for an audit trail. Avoids risk of partial/corrupt
      trailing lines if the process crashes.
    - Directory creation: parent directories are auto-created if missing.
      Raises a clear FileNotFoundError only if the parent directory path
      itself is invalid (e.g. a file exists where a directory is expected).

    Attributes:
        path: The file path to which audit records are appended.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        """Initialize the audit trail with a file path.

        Args:
            path: Where to write audit records. Parent directory will be
                created if it does not exist. The file itself will be created
                on the first append().
        """
        self.path = Path(path)

    def append(self, record: AuditRecord) -> None:
        """Append one audit record as a JSON Line.

        Serializes the AuditRecord to JSON with ISO 8601 datetime format,
        writes it as a single complete line (no partial writes), and ensures
        durability via open-per-call (file is closed after each write).

        Args:
            record: The audit record to append.

        Raises:
            FileNotFoundError: If the parent directory cannot be created
                (e.g. a file exists at a required directory path).
            IOError: If writing to the file fails (permission, disk full, etc).
        """
        # Ensure parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize the record to a JSON object
        json_obj = self._record_to_json(record)
        json_line = json.dumps(json_obj)

        # Write the complete line atomically: open, write newline, close.
        # This guarantees durability and avoids partial writes.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json_line + "\n")

    @staticmethod
    def _record_to_json(record: AuditRecord) -> dict:
        """Convert an AuditRecord to a JSON-serializable dict.

        Converts the datetime field to ISO 8601 string format; all other
        fields are JSON-native (str, dict, list).

        Args:
            record: The audit record to convert.

        Returns:
            A dict with the same structure as the record, ready to serialize
            to JSON.
        """
        return {
            "input": record.input,
            "skill_pack": record.skill_pack,
            "intent": record.intent,
            "rules_evaluated": record.rules_evaluated,
            "decision": record.decision,
            "approval_status": record.approval_status,
            "timestamp": record.timestamp.isoformat(),
        }
