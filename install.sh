#!/usr/bin/env bash
# NEXXUS Crypto Bot Installer
# Creates a desktop launcher with icon and installs all Python dependencies.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || echo "")"
PIP="$(command -v pip3 || echo "")"
APP_NAME="NEXXUS Crypto Bot"
DESKTOP_DIR="$HOME/Desktop"
APPS_DIR="$HOME/.local/share/applications"
ICON_SRC="$SCRIPT_DIR/icon.png"
ICON_DEST="$HOME/.local/share/icons/nexxus-crypto-bot.png"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   NEXXUS Crypto Bot — Installer      ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Python check ─────────────────────────────────────────────────────────────
if [ -z "$PYTHON" ]; then
  echo "✗  python3 not found. Please install Python 3.9+ first."
  exit 1
fi

PY_VER=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "✓  Python $PY_VER found at $PYTHON"

if [ -z "$PIP" ]; then
  echo "✗  pip3 not found. Please install pip first."
  exit 1
fi

# ── Dependencies ──────────────────────────────────────────────────────────────
echo ""
echo "Installing Python dependencies..."
$PIP install --quiet --upgrade \
  customtkinter \
  pillow \
  coinbase-advanced-py \
  matplotlib \
  websockets \
  numpy \
  pytz

echo "✓  Dependencies installed"

# ── Icon ──────────────────────────────────────────────────────────────────────
mkdir -p "$HOME/.local/share/icons"
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$ICON_DEST"
  echo "✓  Icon installed"
else
  echo "⚠  icon.png not found — desktop entry will use default icon"
fi

# ── .desktop file ─────────────────────────────────────────────────────────────
mkdir -p "$APPS_DIR"

cat > "$APPS_DIR/nexxus-crypto-bot.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=NEXXUS Crypto Bot
GenericName=Cryptocurrency Trading Bot
Comment=Automated crypto trading bot with live charts and SMC strategy
Exec=$PYTHON "$SCRIPT_DIR/main.py"
Icon=$ICON_DEST
Terminal=false
Categories=Finance;Office;
Keywords=crypto;bitcoin;trading;coinbase;bot;
StartupWMClass=NEXXUS
StartupNotify=true
EOF

chmod +x "$APPS_DIR/nexxus-crypto-bot.desktop"
echo "✓  App entry created at $APPS_DIR/nexxus-crypto-bot.desktop"

# ── Desktop shortcut ──────────────────────────────────────────────────────────
if [ -d "$DESKTOP_DIR" ]; then
  cp "$APPS_DIR/nexxus-crypto-bot.desktop" "$DESKTOP_DIR/nexxus-crypto-bot.desktop"
  chmod +x "$DESKTOP_DIR/nexxus-crypto-bot.desktop"
  # Mark as trusted on GNOME / KDE
  gio set "$DESKTOP_DIR/nexxus-crypto-bot.desktop" \
    metadata::trusted true 2>/dev/null || true
  echo "✓  Desktop shortcut created"
fi

# ── Update icon cache ─────────────────────────────────────────────────────────
if command -v gtk-update-icon-cache &>/dev/null; then
  gtk-update-icon-cache -f "$HOME/.local/share/icons" 2>/dev/null || true
fi
if command -v update-desktop-database &>/dev/null; then
  update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Installation complete!             ║"
echo "║                                      ║"
echo "║   Run:  python3 \"$SCRIPT_DIR/main.py\""
echo "║   Or double-click the desktop icon   ║"
echo "╚══════════════════════════════════════╝"
echo ""
