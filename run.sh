#!/usr/bin/env bash
# One-command launch: installs the http extra if missing, loads a local
# .env (e.g. EAIS_LLM_* for Groq) without overriding vars already set in
# the shell, starts the web server, and opens the dashboard in a browser.
# See README.md's "Run everything with one command" section.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if ! python -c "import flask" >/dev/null 2>&1; then
    echo "Installing eais-scheduling-agent with the http extra..."
    python -m pip install -e ".[http]"
fi

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

echo "Starting EAIS Scheduling Agent at http://127.0.0.1:5000 (Ctrl+C to stop) ..."
(
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open http://127.0.0.1:5000/ >/dev/null 2>&1
    elif command -v open >/dev/null 2>&1; then
        open http://127.0.0.1:5000/
    fi
) &

python -c "from eais_scheduling_agent.http_api import run; run()"
