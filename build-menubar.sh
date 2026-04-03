#!/usr/bin/env bash
# build-menubar.sh — Build & install the CCP LiteLLM menu bar app
#
# Usage: bash build-menubar.sh [--no-launch]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWIFT_SRC="$SCRIPT_DIR/CcpMenuBar.swift"
BINARY_NAME="CcpMenuBar"
APP_DIR="$HOME/Applications/CCP LiteLLM.app"
APP_MACOS="$APP_DIR/Contents/MacOS"
APP_BINARY="$APP_MACOS/$BINARY_NAME"
CCP_BIN="$HOME/.local/bin/ccp-litellm"

PROXY_PLIST="$HOME/Library/LaunchAgents/com.ccp.litellm.proxy.plist"
MENUBAR_PLIST="$HOME/Library/LaunchAgents/com.ccp.litellm.menubar.plist"

NO_LAUNCH=false
for arg in "$@"; do
    [[ "$arg" == "--no-launch" ]] && NO_LAUNCH=true
done

echo "╔══════════════════════════════════════════╗"
echo "║   CCP LiteLLM — Build & Install          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------------------
# 1. Compile Swift
# ---------------------------------------------------------------------------
echo "▸ Compiling $SWIFT_SRC ..."

if ! command -v swiftc &>/dev/null; then
    echo "  ERROR: swiftc not found. Install Xcode Command Line Tools:"
    echo "         xcode-select --install"
    exit 1
fi

SWIFTC_TMP="$SCRIPT_DIR/${BINARY_NAME}_build"
swiftc \
    -framework Cocoa \
    -O \
    -module-name CcpMenuBar \
    "$SWIFT_SRC" \
    -o "$SWIFTC_TMP"

echo "  OK: compiled → $SWIFTC_TMP"
echo ""

# ---------------------------------------------------------------------------
# 2. Create .app bundle
# ---------------------------------------------------------------------------
echo "▸ Creating app bundle: $APP_DIR"

mkdir -p "$APP_MACOS"
cp "$SWIFTC_TMP" "$APP_BINARY"
chmod 755 "$APP_BINARY"
rm "$SWIFTC_TMP"

# Info.plist
cat > "$APP_DIR/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.ccp.litellm.menubar</string>
    <key>CFBundleName</key>
    <string>CCP LiteLLM</string>
    <key>CFBundleDisplayName</key>
    <string>CCP LiteLLM</string>
    <key>CFBundleExecutable</key>
    <string>CcpMenuBar</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
</dict>
</plist>
PLIST

echo "  OK: bundle at $APP_DIR"
echo ""

# ---------------------------------------------------------------------------
# 3. LaunchAgent — proxy server
# ---------------------------------------------------------------------------
echo "▸ Writing proxy LaunchAgent: $PROXY_PLIST"
mkdir -p "$(dirname "$PROXY_PLIST")"

cat > "$PROXY_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ccp.litellm.proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>${CCP_BIN}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/ccp-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ccp-server.log</string>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/Users/jeremiah/.local/bin</string>
    </dict>
</dict>
</plist>
PLIST

echo "  OK: $PROXY_PLIST"

# ---------------------------------------------------------------------------
# 4. LaunchAgent — menu bar app
# ---------------------------------------------------------------------------
echo "▸ Writing menu bar LaunchAgent: $MENUBAR_PLIST"

cat > "$MENUBAR_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ccp.litellm.menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP_BINARY}</string>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
PLIST

echo "  OK: $MENUBAR_PLIST"
echo ""

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo "╔══════════════════════════════════════════╗"
echo "║   Installation Complete                  ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  App:        $APP_DIR"
echo "  Binary:     $APP_BINARY"
echo "  Proxy plist: $PROXY_PLIST"
echo "  Menu plist:  $MENUBAR_PLIST"
echo ""
echo "  Enable start-at-login via the menu bar app toggles, or:"
echo "    launchctl bootstrap gui/\$(id -u) $PROXY_PLIST"
echo "    launchctl bootstrap gui/\$(id -u) $MENUBAR_PLIST"
echo ""

# ---------------------------------------------------------------------------
# 6. Launch (optional)
# ---------------------------------------------------------------------------
if $NO_LAUNCH; then
    echo "  (skipping launch — use: open \"$APP_DIR\")"
    exit 0
fi

echo "▸ Killing any existing instance..."
pkill -f "$BINARY_NAME" 2>/dev/null && sleep 0.5 || true

echo "▸ Launching $APP_DIR ..."
open "$APP_DIR"
echo "  Done — look for the status icon in your menu bar."
