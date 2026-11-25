@echo off
REM One-click start for Windows: creates the virtual environment on first run.
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || goto :error
  .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
.venv\Scripts\python -m server.main %*
goto :eof
:error
echo Setup failed. Is Python 3.11+ installed and on PATH?
pause
