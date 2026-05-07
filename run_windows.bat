@echo off
:: NEXXUS Crypto Bot — Windows launcher
:: Runs via the local .venv so no system-level Python install is required
:: after setup.  Uses pythonw.exe to suppress the console window.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%~dp0main.py"
) else (
    python "%~dp0main.py"
)
