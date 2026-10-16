@echo off
rem Opens the AgentDesk dashboard (http://127.0.0.1:8765). Other commands: AgentDesk.cmd run PROJECT, ...
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" -m agentdesk %*
