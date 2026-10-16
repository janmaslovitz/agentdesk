@echo off
rem Opens the AgentDesk dashboard (http://127.0.0.1:8765) with no console window; it keeps running after this closes.
rem Stop it with: AgentDesk.cmd stop   Other commands: AgentDesk.cmd run PROJECT, ...
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
if exist ".venv\Scripts\pythonw.exe" (set "PYW=.venv\Scripts\pythonw.exe") else (set "PYW=pythonw")
if "%~1"=="" (
  start "" "%PYW%" -m agentdesk
) else (
  "%PY%" -m agentdesk %*
)
