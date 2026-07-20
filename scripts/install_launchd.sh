#!/bin/sh
set -eu

LABEL="com.local.amazon-payout-api"
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3)}
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="$HOME/Library/Application Support/AmazonPayoutConsole"
LOG_DIR="$RUNTIME_DIR/logs"

mkdir -p "$HOME/Library/LaunchAgents" "$RUNTIME_DIR" "$LOG_DIR"
rm -rf "$RUNTIME_DIR/amazon_payout_api" "$RUNTIME_DIR/web"
cp -R "$PROJECT_DIR/amazon_payout_api" "$RUNTIME_DIR/amazon_payout_api"
cp -R "$PROJECT_DIR/web" "$RUNTIME_DIR/web"
cp "$PROJECT_DIR/.env" "$RUNTIME_DIR/.env"
chmod 600 "$RUNTIME_DIR/.env"
if [ ! -d "$RUNTIME_DIR/data" ]; then
  if [ -d "$PROJECT_DIR/data" ]; then
    cp -R "$PROJECT_DIR/data" "$RUNTIME_DIR/data"
  else
    mkdir -p "$RUNTIME_DIR/data"
  fi
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>-m</string>
    <string>amazon_payout_api.server</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$RUNTIME_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>$RUNTIME_DIR</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/server.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/server-error.log</string>
</dict>
</plist>
EOF

chmod 600 "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

printf 'Installed %s\n' "$PLIST"
printf 'Runtime: %s\n' "$RUNTIME_DIR"
printf 'Console: http://127.0.0.1:8080\n'
