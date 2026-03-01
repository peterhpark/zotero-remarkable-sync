#!/bin/bash
# build_app.sh — Build a minimal .app bundle for ZoteroReMarkable menu bar app.
#
# Uses a compiled C launcher (not a shell script) so that macOS Launch Services
# properly registers the .app bundle with the window server before exec-ing Python.

set -euo pipefail

APP="/Applications/ZoteroReMarkable.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
SCRIPT_DIR="$HOME/Scripts/zotero-remarkable"
PYTHON="/opt/homebrew/bin/python3"

# Verify prerequisites
if [ ! -f "$SCRIPT_DIR/zotero_rm_app.py" ]; then
    echo "ERROR: $SCRIPT_DIR/zotero_rm_app.py not found" >&2
    exit 1
fi
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: $PYTHON not found or not executable" >&2
    exit 1
fi

# Ensure rumps is installed
"$PYTHON" -c "import rumps" 2>/dev/null || {
    echo "Installing rumps..."
    "$PYTHON" -m pip install rumps --break-system-packages
}

# Remove old bundle if it exists
if [ -d "$APP" ]; then
    echo "Removing old $APP ..."
    rm -rf "$APP"
fi

# Create directory structure
echo "Creating $APP ..."
mkdir -p "$MACOS" "$RESOURCES"

# Write Info.plist
cat > "$CONTENTS/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ZoteroReMarkable</string>
    <key>CFBundleDisplayName</key>
    <string>ZoteroReMarkable</string>
    <key>CFBundleIdentifier</key>
    <string>com.user.zotero-remarkable-app</string>
    <key>CFBundleExecutable</key>
    <string>ZoteroReMarkable</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# Write PkgInfo
echo -n "APPL????" > "$CONTENTS/PkgInfo"

# Compile native launcher
# A compiled Mach-O binary ensures Launch Services properly registers the process.
# The Python script sets NSApplicationActivationPolicyAccessory before rumps starts,
# which handles the LSUIElement/Dock hiding that our plist can't enforce after exec.
LAUNCHER_SRC=$(mktemp /tmp/zrm_launcher.XXXXXX.c)
cat > "$LAUNCHER_SRC" << 'CSRC'
#include <spawn.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/wait.h>

extern char **environ;

int main(int argc, char *argv[]) {
    const char *home = getenv("HOME");
    if (!home) home = "/Users/peterhpark";

    char script[1024];
    snprintf(script, sizeof(script),
             "%s/Scripts/zotero-remarkable/zotero_rm_app.py", home);

    char *child_argv[] = {
        "/opt/homebrew/bin/python3",
        script,
        NULL
    };

    pid_t pid;
    int status = posix_spawn(&pid, "/opt/homebrew/bin/python3",
                             NULL, NULL, child_argv, environ);
    if (status != 0) {
        perror("posix_spawn failed");
        return 1;
    }

    /* Keep the original .app process alive so Launch Services
       maintains the window-server registration.  */
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
CSRC

echo "Compiling launcher..."
cc -o "$MACOS/ZoteroReMarkable" "$LAUNCHER_SRC" -arch arm64
rm -f "$LAUNCHER_SRC"

echo "Done. Built $APP"
echo ""
echo "To launch:"
echo "  open /Applications/ZoteroReMarkable.app"
echo ""
echo "If Gatekeeper blocks it:"
echo "  xattr -cr /Applications/ZoteroReMarkable.app"
