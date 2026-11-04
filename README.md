# iphone-wired-relay

Use iPhones plugged into one Mac from Xcode on another Mac, with traffic on
cables instead of Wi-Fi.

```
MacBook (Xcode) ── cable ── Mac mini (this daemon) ── USB ── iPhone
```

A USB-attached iPhone advertises `_remotepairing._tcp` on its USB network
interface, the same service Xcode uses for wireless debugging. The daemon runs
on the Mac the phones plug into. It republishes that service on the direct
Mac-to-Mac link and relays connections, so the other Mac sees a network device.
The Mac running Xcode needs no setup.

This relies on undocumented behavior of Xcode and iOS, and an update may break
it. Forwarding usbmuxd does not work for this: Xcode's protocol on iOS 17 and
later is not carried by usbmuxd.

## Requirements

- A direct cable between the two Macs that gives both a self-assigned
  `169.254.x.x` address. USB-C at 10 Gb/s and Thunderbolt both qualify.
- Each phone already paired with the Mac running Xcode.
- The relay host logged in, since this is a LaunchAgent. No sudo is needed.

## Install

```sh
./install.sh <ssh-host>            # install and start
./install.sh <ssh-host> uninstall  # stop and remove
```

Logs are in `~/Library/Logs/iphone-relay.log` on the relay host.

## Behavior

- Follows phones as they appear and as their Bonjour identifier rotates, which
  happens after a tunnel closes.
- Follows the link interface and its address.
- Publishes only on the link interface, never on the LAN.
- Holds listeners only while a phone is present, plus a 60 second grace period.

## Limitations

- While a phone is present, the relay host has only 1024 ephemeral IPv4 ports
  for its own outgoing connections. See the comment on `TUNNEL_PORTS`. Moving
  `net.inet.ip.portrange.first` and `last` below 49152 with sudo would remove
  this, and the bound range could then grow to all of 49152-65535.
- A phone that picks a tunnel port in that free top range cannot be reached on
  that attempt.
- Throughput is capped by each phone's USB link. A 480 Mb/s link measured about
  35 MB/s.

## Test

```sh
python3 -m unittest
```
