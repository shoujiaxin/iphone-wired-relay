#!/bin/sh
# Installs or removes the bridge LaunchDaemon on the Mac the phones plug into.
# Usage: install.sh <ssh-host> [uninstall]
# Both actions ask for that Mac's sudo password.
set -eu

host="${1:?usage: install.sh <ssh-host> [uninstall]}"
action="${2:-install}"
label="me.jiaxin.iphone-bridge"
script="/usr/local/libexec/iphone-bridge.py"
plist="/Library/LaunchDaemons/$label.plist"
log="/var/log/iphone-bridge.log"
source_dir="$(cd "$(dirname "$0")" && pwd)"

if [ "$action" = "uninstall" ]; then
    ssh -t "$host" "sudo sh -c '
        launchctl bootout system/$label 2>/dev/null
        rm -f $plist $script $log
        ifconfig bridge200 destroy 2>/dev/null
        for member in \$(ifconfig bridge0 2>/dev/null | grep -o member:.en[0-9]* | cut -c9-); do
            ifconfig -v \$member | grep -q USB.Ethernet && ifconfig bridge0 deletem \$member
        done
        true'"
    echo "removed from $host"
    exit 0
fi

staging="$(ssh "$host" mktemp -d)"
trap 'ssh "$host" rm -rf "$staging"' EXIT
scp -q "$source_dir/iphone_bridge.py" "$source_dir/$label.plist" "$host:$staging/"
ssh -t "$host" "sudo sh -c '
    set -e
    mkdir -p /usr/local/libexec
    install -o root -g wheel -m 755 $staging/iphone_bridge.py $script
    install -o root -g wheel -m 644 $staging/$label.plist $plist
    launchctl bootout system/$label 2>/dev/null || true
    launchctl bootstrap system $plist'"
echo "installed on $host"
