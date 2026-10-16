#!/bin/sh
# Opens the AgentDesk dashboard (http://127.0.0.1:8765) in the background; it keeps running after the terminal closes.
# Stop it with: ./agentdesk.sh stop   Other commands: ./agentdesk.sh run PROJECT, ...
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
if [ $# -eq 0 ]; then
  mkdir -p data
  if command -v setsid >/dev/null 2>&1; then
    nohup setsid "$PY" -m agentdesk >> data/agentdesk.log 2>&1 &
  else
    nohup "$PY" -m agentdesk >> data/agentdesk.log 2>&1 &
  fi
  exit 0
fi
exec "$PY" -m agentdesk "$@"
