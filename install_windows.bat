@echo off
:: NEXXUS Crypto Bot — Windows Installer launcher
:: Right-click → Run as administrator is NOT required.
:: This batch file calls the PowerShell installer with a bypassed execution
:: policy so Windows security prompts don't block the install.

echo.
echo  Starting NEXXUS Crypto Bot installer...
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"

if %errorlevel% neq 0 (
    echo.
    echo  Installation encountered an error. See messages above.
    pause
)
