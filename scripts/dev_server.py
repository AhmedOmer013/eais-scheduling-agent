"""Dev-server launcher used by `.claude/launch.json` (Claude Code's preview
tool) to start the dashboard. Equivalent to what `run.ps1`/`run.sh` already
do -- loads `EAIS_LLM_*` from a local `.env` next to this repo (if present)
without overriding anything already set in the shell, then starts the Flask
dev server -- as a plain script instead of an inline `python -c` one-liner,
since the one-liner's nested quoting did not survive being routed through
the preview tool's process launcher on Windows.
"""

import os
import re
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            name, value = match.groups()
            os.environ.setdefault(name, value)


if __name__ == "__main__":
    _load_dotenv(_ENV_PATH)
    from eais_scheduling_agent.http_api import run

    run()
