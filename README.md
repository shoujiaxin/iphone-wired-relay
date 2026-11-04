# iphone-wired-relay

Use iPhones plugged into one Mac from Xcode on another Mac, with traffic on
cables instead of Wi-Fi.

```
MacBook (Xcode) ── cable ── Mac mini (bridge) ── USB ── iPhone
```

A USB-attached iPhone advertises `_remotepairing._tcp` on its USB network
interface, the same service Xcode uses for wireless debugging. A small root
daemon on the Mac the phones plug into keeps that interface and the direct
Mac-to-Mac link in one Ethernet bridge. The other Mac then receives the phone's
own advert and connects straight to the phone, so it sees a network device. The
Mac running Xcode needs no setup.

This relies on undocumented behavior of Xcode, iOS, and macOS, and an update may
break it. Forwarding usbmuxd does not work for this: Xcode's protocol on iOS 17
and later is not carried by usbmuxd.

## Requirements

- A direct cable between the two Macs: Thunderbolt, or USB-C that gives both a
  self-assigned `169.254.x.x` address on an `en` interface. Leave the Thunderbolt
  Bridge network service in place; nothing here changes it.
- Each phone already paired with the Mac running Xcode.
- SSH access and sudo rights on the Mac the phones plug into.

## Install

Run this from the Mac running Xcode. It asks for the remote sudo password.

```sh
./install.sh <ssh-host>
```

This installs:

| Path on the remote Mac | Purpose |
|---|---|
| `/usr/local/libexec/iphone-bridge.py` | The daemon |
| `/Library/LaunchDaemons/me.jiaxin.iphone-bridge.plist` | Starts it as root |
| `/var/log/iphone-bridge.log` | Its log |

To check it, plug a phone into the remote Mac and run:

```sh
ssh <ssh-host> 'cat /var/log/iphone-bridge.log; ifconfig bridge200'
xcrun devicectl list devices
```

The phone should show as `connected`. On a USB-C cable, `bridge200` should list
the link and the phone interface as members. On a Thunderbolt cable there is no
`bridge200`; run `ifconfig bridge0` instead, which should list the phone
interface next to the Thunderbolt ports. Run `install.sh` again to update the
daemon.

## Uninstall

```sh
./install.sh <ssh-host> uninstall
```

This stops the daemon, takes phones out of `bridge0`, destroys `bridge200`, and
removes the files above. It also asks for the remote sudo password.

## Behavior

- Checks every 5 seconds and whenever an advert changes, and joins whatever is
  missing from the bridge.
- Builds the bridge only when a link and at least one phone are both present.
- Rejoins a replugged phone on its own, and rebuilds the bridge if it goes away.
- Identifies phones by their advert, on an interface with no IPv4 address. The
  private Mac-to-Mac link looks the same but has no advert, so it stays out.
- Never bridges to the LAN or Wi-Fi, apart from the first limitation below.
- On a USB-C cable, uses `bridge200` because configd destroys any bridge it does
  not know about whenever an interface attaches, unless the unit has three
  digits.
- On a Thunderbolt cable, adds phones to the system's `bridge0`, which keeps its
  ports, its address, and the Mac-to-Mac connection unchanged. Thunderbolt wins
  if both cables are in, and phones move when the active cable changes.

## Limitations

- **USB-C link detection is a heuristic.** The link is the first active `en`
  interface with a `169.254.x.x` address. A LAN port that failed DHCP also
  matches, and phones would then be bridged onto that network.
- **The remote Mac loses bridged phones.** Its own `devicectl` stops listing a
  phone once that phone joins the bridge, so Xcode on that Mac cannot use it.
- **Thunderbolt connections can blip.** configd removes phones from `bridge0`
  whenever any interface attaches, and the daemon restores them within 5 seconds.
- **Speed is capped by each phone's USB link.** A 480 Mb/s link measured about
  35 MB/s on both a USB-C 10 Gb/s and a 40 Gb/s Thunderbolt link.
- **Stopping the daemon leaves the bridge up.** Only uninstalling or rebooting
  removes it.

Not tested yet:

- Switching between Thunderbolt and USB-C while the daemon runs. Unit tests
  cover the logic, and this version has only run on a Thunderbolt cable.
- More than one phone.
- Replugging the cable between the Macs without changing its type.
- Rebooting the remote Mac. The daemon should start at boot without a login.
- A phone that is also on Wi-Fi. Xcode may prefer Wi-Fi.
- First-time pairing through the bridge.

## Test

```sh
python3 -m unittest
```
