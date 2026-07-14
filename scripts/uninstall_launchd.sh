#!/bin/sh
set -eu

LABEL="com.local.amazon-payout-api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
printf 'Removed %s\n' "$LABEL"
printf 'Runtime data retained in %s\n' "$HOME/Library/Application Support/AmazonPayoutConsole"
