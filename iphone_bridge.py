#!/usr/bin/env python3
"""Make iPhones plugged into this Mac usable from a second Mac over a cable.

A USB-attached iPhone advertises _remotepairing._tcp on its USB network
interface, the same service Xcode uses for wireless debugging. This daemon keeps
that interface and the direct Mac-to-Mac link in one bridge, so the other Mac
receives the phone's own advert and connects straight to the phone. The other
Mac needs no setup, and this Mac only forwards Ethernet frames.

A USB cable shows up as a plain interface, which joins a bridge of our own. A
Thunderbolt cable already belongs to the system's Thunderbolt Bridge, which also
holds this Mac's address on that link, so phones join that bridge instead.

Changing bridge membership needs root.
"""
import dataclasses
import os
import re
import select
import socket
import subprocess
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

# configd destroys every bridge missing from its preferences when an interface
# attaches, which a phone replug triggers, but it leaves three-digit units
# alone. Internet Sharing and vmnet take their units from bridge100 upward.
BRIDGE = "bridge200"
# configd also removes members it does not know from this bridge when an
# interface attaches. It leaves the bridge and its ports alone, and the next
# poll puts the phone back.
THUNDERBOLT_BRIDGE = "bridge0"
LINK_PREFIX = "169.254."
POLL_SECONDS = 5
BROWSE_COMMAND = ["/usr/bin/dns-sd", "-B", "_remotepairing._tcp", "local"]
BROWSE_LINE = re.compile(r"^\S+\s+(Add|Rmv)\s+\d+\s+(\d+)\s+\S+\s+\S+\s+(.+)$")

Advert = Tuple[int, str]  # Interface index and Bonjour instance name.


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


@dataclasses.dataclass
class Interface:
    name: str
    active: bool = False
    ipv4: Optional[str] = None
    members: List[str] = dataclasses.field(default_factory=list)
    thunderbolt: bool = False


def parse_ifconfig(text: str) -> Dict[str, Interface]:
    interfaces: Dict[str, Interface] = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^([a-z]+[0-9]+): flags=", line)
        if header:
            current = interfaces.setdefault(header[1], Interface(header[1]))
        elif current is not None:
            address = re.match(r"\s+inet (\S+)", line)
            member = re.match(r"\s+member: (\S+)", line)
            if address:
                current.ipv4 = address[1]
            elif member:
                current.members.append(member[1])
            elif re.match(r"\s+status: active", line):
                current.active = True
            elif re.match(r"\s+type: IP over Thunderbolt", line):
                current.thunderbolt = True
    return interfaces


def pick_link(interfaces: Dict[str, Interface]) -> Optional[Interface]:
    """Return the Mac-to-Mac link, the first active self-assigned en interface."""
    for name in sorted(interfaces):
        interface = interfaces[name]
        if (
            name.startswith("en")
            and interface.active
            and (interface.ipv4 or "").startswith(LINK_PREFIX)
        ):
            return interface
    return None


def has_thunderbolt_link(interfaces: Dict[str, Interface]) -> bool:
    return any(i.thunderbolt and i.active for i in interfaces.values())


def is_phone_interface(interface: Optional[Interface]) -> bool:
    """A phone's USB interface carries only IPv6 link-local addresses."""
    return interface is not None and interface.ipv4 is None


def apply_browse_line(line: str, adverts: Set[Advert]) -> None:
    match = BROWSE_LINE.match(line)
    if match:
        advert = (int(match[2]), match[3])
        if match[1] == "Add":
            adverts.add(advert)
        else:
            adverts.discard(advert)


def interface_names(adverts: Iterable[Advert]) -> Set[str]:
    names = set()
    for index, _ in adverts:
        try:
            names.add(socket.if_indextoname(index))
        except OSError:
            pass  # The interface left before dns-sd reported the removal.
    return names


def plan(interfaces: Dict[str, Interface], advertised: Set[str]) -> List[List[str]]:
    """Return the ifconfig arguments that bring the bridge up to date.

    Phones are recognized by their advert because the private Mac-to-Mac link
    has the same interface flags and no IPv4 address either. Detached members
    need no handling, since the kernel drops them from the bridge. A phone can
    be in one bridge only, so it leaves the other one first when cables change.
    """
    phones = sorted(
        name for name in advertised if is_phone_interface(interfaces.get(name))
    )
    if not phones:
        return []
    bridge = interfaces.get(BRIDGE)
    system_bridge = interfaces.get(THUNDERBOLT_BRIDGE)
    system_members = system_bridge.members if system_bridge else []

    if system_bridge and has_thunderbolt_link(interfaces):
        leave = [[BRIDGE, "destroy"]] if bridge else []
        return leave + [
            [THUNDERBOLT_BRIDGE, "addm", name]
            for name in phones
            if name not in system_members
        ]

    leave = [
        [THUNDERBOLT_BRIDGE, "deletem", name]
        for name in phones
        if name in system_members
    ]
    link = pick_link(interfaces)
    if link is None:
        return leave
    members = bridge.members if bridge else []
    joins = [
        [BRIDGE, "addm", name] for name in [link.name, *phones] if name not in members
    ]
    if bridge:
        return leave + joins
    return leave + [[BRIDGE, "create"], *joins, [BRIDGE, "up"]]


def read_interfaces() -> Dict[str, Interface]:
    output = subprocess.run(
        ["/sbin/ifconfig", "-a", "-v"], capture_output=True, text=True, check=True
    )
    return parse_ifconfig(output.stdout)


def reconcile(adverts: Set[Advert], failures: Set[str]) -> None:
    for arguments in plan(read_interfaces(), interface_names(adverts)):
        command = " ".join(arguments)
        result = subprocess.run(
            ["/sbin/ifconfig", *arguments], capture_output=True, text=True
        )
        if result.returncode == 0:
            log("ifconfig", command)
            failures.discard(command)
            continue
        # Retried on every poll, so report a lasting failure only once.
        if command not in failures:
            log("ifconfig", command, "failed:", result.stderr.strip())
            failures.add(command)
        return


def main():
    browse = subprocess.Popen(BROWSE_COMMAND, stdout=subprocess.PIPE)
    stream = browse.stdout.fileno()
    adverts: Set[Advert] = set()
    failures: Set[str] = set()
    pending = b""
    log("started")
    while True:
        # The timeout matters: the link can change without any advert changing.
        if select.select([stream], [], [], POLL_SECONDS)[0]:
            data = os.read(stream, 65536)
            if not data:
                raise SystemExit("dns-sd exited")  # launchd restarts the browse.
            *lines, pending = (pending + data).split(b"\n")
            for line in lines:
                apply_browse_line(line.decode(errors="replace"), adverts)
        reconcile(adverts, failures)


if __name__ == "__main__":
    main()
