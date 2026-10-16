#!/bin/sh
# Opens the AgentDesk dashboard (http://127.0.0.1:8765). Other commands: ./agentdesk.sh run PROJECT, ...
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" -m agentdesk "$@"
