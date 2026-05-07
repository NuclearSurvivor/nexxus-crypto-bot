#Requires -Version 5.0
<#
.SYNOPSIS
    NEXXUS Crypto Bot — Windows 10/11 Installer
.DESCRIPTION
    Creates a Python virtual environment, installs all dependencies,
    converts the icon, writes a launch script, and pins desktop +
    Start Menu shortcuts.  Does NOT require administrator privileges.
#>

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── Helpers ───────────────────────────────────────────────────────────────────
function Write-Banner {
    Write-Host ""
    Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  ║   NEXXUS Crypto Bot  —  Windows Installer    ║" -ForegroundColor Cyan
    Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}
function Step($msg)  { Write-Host "  →  $msg" -ForegroundColor White }
function OK($msg)    { Write-Host "  ✓  $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "  ⚠  $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "  ✗  $msg" -ForegroundColor Red }

Write-Banner

# ── 1. Locate Python 3.9+ ─────────────────────────────────────────────────────
Step "Checking Python installation..."

$PythonExePath = $null
$SearchCmds = @("python", "python3", "py")

foreach ($cmd in $SearchCmds) {
    try {
        $verStr = & $cmd --version 2>&1
        if ($verStr -match "Python (\d+)\.(\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -eq 3 -and $min -ge 9) {
                # Resolve full path so venv creation is reliable
                $PythonExePath = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
                if (-not $PythonExePath) { $PythonExePath = $cmd }
                OK "Python $maj.$min found  ($PythonExePath)"
                break
            } else {
                Warn "Python $maj.$min found but 3.9 or newer is required — skipping"
            }
        }
    } catch { }
}

if (-not $PythonExePath) {
    Fail "Python 3.9+ not found on PATH."
    Write-Host ""
    Write-Host "  Download the latest Python 3 installer from:" -ForegroundColor Yellow
    Write-Host "    https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  During install, tick  'Add Python to PATH'  then re-run this script." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

# ── 2. Virtual environment ────────────────────────────────────────────────────
$VenvDir    = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPyw    = Join-Path $VenvDir "Scripts\pythonw.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"

if (Test-Path $VenvPython) {
    OK "Virtual environment already exists — skipping creation"
} else {
    Step "Creating virtual environment in .venv ..."
    & $PythonExePath -m venv $VenvDir
    OK "Virtual environment created"
}

# ── 3. Upgrade pip silently ───────────────────────────────────────────────────
Step "Upgrading pip..."
& $VenvPython -m pip install --upgrade pip --quiet
OK "pip is up to date"

# ── 4. Install Python dependencies ────────────────────────────────────────────
Step "Installing dependencies — this may take a minute..."
$Pkgs = @(
    "customtkinter",
    "pillow",
    "coinbase-advanced-py",
    "matplotlib",
    "websockets",
    "numpy",
    "pytz"
)
& $VenvPip install --quiet --upgrade $Pkgs
OK "All dependencies installed"

# ── 5. Convert icon.png → icon.ico (multi-size for crisp display at any DPI) ──
$IconPng = Join-Path $ProjectDir "icon.png"
$IconIco = Join-Path $ProjectDir "icon.ico"

if (Test-Path $IconPng) {
    if (-not (Test-Path $IconIco)) {
        Step "Converting icon.png → icon.ico..."
        $iconScript = @"
from PIL import Image
img = Image.open(r'$IconPng').convert('RGBA')
img.save(r'$IconIco', format='ICO',
         sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
"@
        try {
            & $VenvPython -c $iconScript
            OK "icon.ico created"
        } catch {
            Warn "Icon conversion failed — shortcuts will use the default Python icon"
        }
    } else {
        OK "icon.ico already exists"
    }
} else {
    Warn "icon.png not found — shortcuts will use the default Python icon"
}

$IconLocation = if (Test-Path $IconIco) { "$IconIco,0" } else { "$VenvPyw,0" }

# ── 6. Write run_windows.bat ──────────────────────────────────────────────────
Step "Writing run_windows.bat..."
$RunBatPath = Join-Path $ProjectDir "run_windows.bat"
$RunBatContent = @"
@echo off
cd /d "%~dp0"
rem Launch without a console window; errors are captured by Python's crash handler.
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%~dp0main.py"
) else (
    python "%~dp0main.py"
)
"@
[System.IO.File]::WriteAllText($RunBatPath, $RunBatContent, (New-Object System.Text.UTF8Encoding $false))
OK "run_windows.bat written"

# ── 7. Desktop shortcut ───────────────────────────────────────────────────────
Step "Creating desktop shortcut..."
try {
    $Shell        = New-Object -ComObject WScript.Shell
    $DesktopPath  = [Environment]::GetFolderPath("Desktop")
    $LnkPath      = Join-Path $DesktopPath "NEXXUS Crypto Bot.lnk"
    $Lnk          = $Shell.CreateShortcut($LnkPath)
    $Lnk.TargetPath      = $VenvPyw
    $Lnk.Arguments       = "`"$ProjectDir\main.py`""
    $Lnk.WorkingDirectory = $ProjectDir
    $Lnk.Description     = "NEXXUS Crypto Trading Bot"
    $Lnk.IconLocation    = $IconLocation
    $Lnk.Save()
    OK "Desktop shortcut created  ($LnkPath)"
} catch {
    Warn "Could not create desktop shortcut: $_"
}

# ── 8. Start Menu shortcut ────────────────────────────────────────────────────
Step "Adding to Start Menu..."
try {
    $SmBase = [Environment]::GetFolderPath("StartMenu")
    $SmDir  = Join-Path $SmBase "Programs\NEXXUS"
    if (-not (Test-Path $SmDir)) { New-Item -ItemType Directory -Path $SmDir | Out-Null }
    $SmLnk  = $Shell.CreateShortcut((Join-Path $SmDir "NEXXUS Crypto Bot.lnk"))
    $SmLnk.TargetPath       = $VenvPyw
    $SmLnk.Arguments        = "`"$ProjectDir\main.py`""
    $SmLnk.WorkingDirectory = $ProjectDir
    $SmLnk.Description      = "NEXXUS Crypto Trading Bot"
    $SmLnk.IconLocation     = $IconLocation
    $SmLnk.Save()
    OK "Start Menu shortcut created"
} catch {
    Warn "Could not create Start Menu shortcut: $_"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║   Installation complete!                      ║" -ForegroundColor Green
Write-Host "  ║                                               ║" -ForegroundColor Green
Write-Host "  ║   How to launch:                              ║" -ForegroundColor Green
Write-Host "  ║     • Double-click 'NEXXUS Crypto Bot'        ║" -ForegroundColor Green
Write-Host "  ║       on your Desktop                         ║" -ForegroundColor Green
Write-Host "  ║     • Or run  run_windows.bat                 ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Read-Host "  Press Enter to close"
