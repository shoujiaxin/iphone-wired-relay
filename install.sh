#!/bin/sh
# Installs or removes the relay LaunchAgent on the Mac the phones plug into.
# Usage: install.sh <ssh-host> [uninstall]
set -eu

host="${1:?usage: install.sh <ssh-host> [uninstall]}"
action="${2:-install}"
label="me.jiaxin.iphone-relay"
source_dir="$(cd "$(dirname "$0")" && pwd)"

if [ "$action" = "uninstall" ]; then
    ssh "$host" "launchctl bootout gui/\$(id -u)/$label 2>/dev/null || true
        rm -f ~/Library/LaunchAgents/$label.plist
        rm -rf ~/.local/share/iphone-relay
        rm -f ~/Library/Logs/iphone-relay.log"
    echo "removed from $host"
    exit 0
fi

ssh "$host" 'mkdir -p ~/.local/share/iphone-relay ~/Library/LaunchAgents'
scp -q "$source_dir/iphone_relay.py" "$host:.local/share/iphone-relay/iphone_relay.py"
ssh "$host" "cat > ~/Library/LaunchAgents/$label.plist <<PLIST
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-u</string>
        <string>\$HOME/.local/share/iphone-relay/iphone_relay.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>\$HOME/Library/Logs/iphone-relay.log</string>
    <key>StandardErrorPath</key><string>\$HOME/Library/Logs/iphone-relay.log</string>
</dict>
</plist>
PLIST
launchctl bootout gui/\$(id -u)/$label 2>/dev/null || true
launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$label.plist"
echo "installed on $host"
