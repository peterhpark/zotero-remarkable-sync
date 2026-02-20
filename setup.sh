#!/bin/bash
# setup.sh — Install and configure Zotero <-> reMarkable sync
#
# Usage: bash setup.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.local/share/zotero-remarkable/venv"
SCRIPT_DIR="$HOME/Scripts/zotero-remarkable"
PLIST_NAME="com.user.zotero-remarkable-sync"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
NOTES_DIR="$HOME/RemarkableNotes"

echo "============================================"
echo " Zotero <-> reMarkable Sync Setup"
echo "============================================"
echo ""

# --- Step 1: Check dependencies ---
echo "▸ Checking dependencies..."

if ! command -v python3 &>/dev/null; then
    echo "  ✗ python3 not found. Install from https://python.org"
    exit 1
fi
echo "  ✓ python3 ($(python3 --version 2>&1))"

if ! command -v rmapi &>/dev/null; then
    echo "  ✗ rmapi not found. Installing via Homebrew..."
    if command -v brew &>/dev/null; then
        brew install rmapi
    else
        echo "  ✗ Homebrew not found. Install rmapi manually:"
        echo "    https://github.com/ddvk/rmapi/releases"
        exit 1
    fi
fi
echo "  ✓ rmapi"

# --- Step 2: Create/recreate virtual environment ---
echo ""
echo "▸ Setting up Python virtual environment..."

mkdir -p "$(dirname "$VENV_DIR")"

if [ -d "$VENV_DIR" ]; then
    echo "  Removing existing venv..."
    rm -rf "$VENV_DIR"
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" --quiet
echo "  ✓ venv created at $VENV_DIR"
echo "  ✓ Dependencies installed (pyzotero, pymupdf)"

# --- Step 3: Check rmapi authentication ---
echo ""
echo "▸ Checking reMarkable Cloud authentication..."
if [ ! -f "$HOME/.rmapi" ] && [ ! -f "$HOME/.config/rmapi/rmapi.conf" ]; then
    echo "  You need to authenticate rmapi with your reMarkable account."
    echo "  Running rmapi now — follow the prompts:"
    echo ""
    rmapi version || rmapi quit 2>/dev/null || true
    echo ""
    echo "  If that didn't work, run 'rmapi' manually and follow the auth flow."
fi
echo "  ✓ rmapi configured"

# --- Step 4: Get Zotero credentials ---
echo ""
echo "▸ Zotero API configuration"
echo "  Get your credentials from: https://www.zotero.org/settings/keys"
echo ""

read -p "  Zotero Library ID (numeric user ID): " ZOT_LIB_ID
read -p "  Zotero API Key: " ZOT_API_KEY

if [ -z "$ZOT_LIB_ID" ] || [ -z "$ZOT_API_KEY" ]; then
    echo "  ✗ Both values are required."
    exit 1
fi

# --- Step 5: Verify Zotero storage path ---
echo ""
ZOTERO_STORAGE="$HOME/Zotero/storage"
if [ ! -d "$ZOTERO_STORAGE" ]; then
    echo "  Default Zotero storage not found at $ZOTERO_STORAGE"
    read -p "  Enter your Zotero storage path: " ZOTERO_STORAGE
    if [ ! -d "$ZOTERO_STORAGE" ]; then
        echo "  ✗ Path does not exist."
        exit 1
    fi
fi
echo "  ✓ Zotero storage: $ZOTERO_STORAGE"

# --- Step 6: Install files ---
echo ""
echo "▸ Installing sync script..."

mkdir -p "$SCRIPT_DIR"
mkdir -p "$NOTES_DIR"

cp "$REPO_DIR/zotero_rm_sync.py" "$SCRIPT_DIR/zotero_rm_sync.py"
chmod +x "$SCRIPT_DIR/zotero_rm_sync.py"
echo "  ✓ Installed $SCRIPT_DIR/zotero_rm_sync.py"

# --- Step 7: Configure launchd ---
echo ""
echo "▸ Setting up automatic sync (every 5 minutes + on folder change)..."

PLIST_SRC="$REPO_DIR/$PLIST_NAME.plist"

# Unload existing agent if present
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Generate plist with actual values
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__VENV__|$VENV_DIR|g" \
    -e "s|__ZOTERO_LIBRARY_ID__|$ZOT_LIB_ID|g" \
    -e "s|__ZOTERO_API_KEY__|$ZOT_API_KEY|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl load "$PLIST_DST"
echo "  ✓ launchd agent installed and loaded"

# --- Step 8: Run a dry-run test ---
echo ""
echo "▸ Running a dry-run test..."
echo ""

ZOTERO_LIBRARY_ID="$ZOT_LIB_ID" \
ZOTERO_API_KEY="$ZOT_API_KEY" \
ZOTERO_STORAGE="$ZOTERO_STORAGE" \
"$VENV_DIR/bin/python" "$SCRIPT_DIR/zotero_rm_sync.py" --dry-run

echo ""
echo "============================================"
echo " Setup complete!"
echo "============================================"
echo ""
echo " HOW TO USE:"
echo ""
echo " 1. In Zotero, tag papers you want on your reMarkable:"
echo "    • rm/Neuroscience       → /Zotero/Neuroscience/"
echo "    • rm/ML/Transformers    → /Zotero/ML/Transformers/"
echo "    • rm/Methods/Stats      → /Zotero/Methods/Stats/"
echo ""
echo " 2. Sync runs automatically every 5 minutes"
echo "    (and instantly when new PDFs appear in Zotero storage)"
echo ""
echo " 3. To also download annotated PDFs back, run manually with:"
echo "    $VENV_DIR/bin/python $SCRIPT_DIR/zotero_rm_sync.py --pull-notes"
echo ""
echo " MANUAL COMMANDS:"
echo "    $VENV_DIR/bin/python $SCRIPT_DIR/zotero_rm_sync.py              # sync now"
echo "    $VENV_DIR/bin/python $SCRIPT_DIR/zotero_rm_sync.py --dry-run    # preview"
echo "    $VENV_DIR/bin/python $SCRIPT_DIR/zotero_rm_sync.py --reset      # re-sync all"
echo ""
echo " LOGS:"
echo "    tail -f $SCRIPT_DIR/sync.log"
echo ""
echo " TO STOP automatic sync:"
echo "    launchctl unload ~/Library/LaunchAgents/$PLIST_NAME.plist"
echo ""
