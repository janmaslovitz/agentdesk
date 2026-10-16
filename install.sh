#!/bin/sh
# AgentDesk setup for macOS and Linux: a private Python environment in .venv next to the app.
# Needs Python 3.11+ and git.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet
chmod +x agentdesk.sh
echo "AgentDesk is ready. Start it with ./agentdesk.sh"
