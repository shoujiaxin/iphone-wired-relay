#!/usr/bin/env python3
"""Make iPhones plugged into this Mac usable from a second Mac over a cable.

A USB-attached iPhone advertises _remotepairing._tcp on its USB network
interface, the same service Xcode uses for wireless debugging. This daemon
republishes that service on the direct Mac-to-Mac link and relays connections
to the phone, so the other Mac sees a network device while traffic stays wired.
The other Mac needs no setup: its remotepairingd connects on its own.

Tunnel ports are the awkward part. After pair-verify the phone allocates a
listener port inside the encrypted control channel, so the relay cannot learn
it and has to listen on nearly the whole ephemeral range.
"""
import asyncio
import ctypes
import dataclasses
import itertools
import re
import resource
import socket
import subprocess
import time
from typing import Dict, List, Optional, Tuple

SERVICE_TYPE = b"_remotepairing._tcp"
LINK_PREFIX = "169.254."
CONTROL_PORT_BASE = 42100

# The kernel treats a port bound on any address as taken when it picks an
# ephemeral port, so binding the full range blocks every outgoing connection of
# that address family on this Mac. Listeners therefore stay on IPv4 and leave
# the top of the range free. Connections to phones use IPv6, which is untouched.
TUNNEL_PORTS = range(49152, 64512)

POLL_SECONDS = 5
# Identifier rotation briefly removes a phone; rebinding every port is slow.
IDLE_TEARDOWN_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 3
BUFFER_BYTES = 1 << 20
MAX_OPEN_FILES = 20000

FLAG_ADD = 0x2
PROTOCOL_IPV6 = 0x2


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


# Interface inspection


@dataclasses.dataclass
class Interface:
    name: str
    active: bool = False
    ipv4: Optional[str] = None


def parse_ifconfig(text: str) -> Dict[str, Interface]:
    interfaces: Dict[str, Interface] = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^([a-z]+[0-9]+): flags=", line)
        if header:
            current = interfaces.setdefault(header[1], Interface(header[1]))
        elif current is not None:
            address = re.match(r"\s+inet (\S+)", line)
            if address:
                current.ipv4 = address[1]
            elif re.match(r"\s+status: active", line):
                current.active = True
    return interfaces


def pick_link(interfaces: Dict[str, Interface]) -> Optional[Interface]:
    """Return the Mac-to-Mac link. Sorting prefers Thunderbolt's bridge0."""
    for name in sorted(interfaces):
        interface = interfaces[name]
        if interface.active and (interface.ipv4 or "").startswith(LINK_PREFIX):
            return interface
    return None


def is_phone_interface(interface: Optional[Interface]) -> bool:
    """A phone's USB interface carries only IPv6 link-local addresses."""
    return interface is not None and interface.ipv4 is None


def read_interfaces() -> Dict[str, Interface]:
    output = subprocess.run(
        ["/sbin/ifconfig", "-a"], capture_output=True, text=True, check=True
    )
    return parse_ifconfig(output.stdout)


# dns_sd bindings

_dns_sd = ctypes.CDLL("/usr/lib/libSystem.dylib")
_Ref = ctypes.c_void_p
_U16, _U32, _I32 = ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int32
_Text, _Pointer = ctypes.c_char_p, ctypes.c_void_p

_BrowseReply = ctypes.CFUNCTYPE(
    None, _Ref, _U32, _U32, _I32, _Text, _Text, _Text, _Pointer
)
_ResolveReply = ctypes.CFUNCTYPE(
    None, _Ref, _U32, _U32, _I32, _Text, _Text, _U16, _U16, _Pointer, _Pointer
)
_AddressReply = ctypes.CFUNCTYPE(
    None, _Ref, _U32, _U32, _I32, _Text, _Pointer, _U32, _Pointer
)
_RegisterReply = ctypes.CFUNCTYPE(None, _Ref, _U32, _I32, _Text, _Text, _Text, _Pointer)

_RefOut = ctypes.POINTER(_Ref)
_dns_sd.DNSServiceRefSockFD.argtypes = [_Ref]
_dns_sd.DNSServiceRefSockFD.restype = ctypes.c_int
_dns_sd.DNSServiceProcessResult.argtypes = [_Ref]
_dns_sd.DNSServiceProcessResult.restype = _I32
_dns_sd.DNSServiceRefDeallocate.argtypes = [_Ref]
_dns_sd.DNSServiceRefDeallocate.restype = None
_dns_sd.DNSServiceBrowse.argtypes = [
    _RefOut, _U32, _U32, _Text, _Text, _BrowseReply, _Pointer,
]
_dns_sd.DNSServiceBrowse.restype = _I32
_dns_sd.DNSServiceResolve.argtypes = [
    _RefOut, _U32, _U32, _Text, _Text, _Text, _ResolveReply, _Pointer,
]
_dns_sd.DNSServiceResolve.restype = _I32
_dns_sd.DNSServiceGetAddrInfo.argtypes = [
    _RefOut, _U32, _U32, _U32, _Text, _AddressReply, _Pointer,
]
_dns_sd.DNSServiceGetAddrInfo.restype = _I32
_dns_sd.DNSServiceRegister.argtypes = [
    _RefOut, _U32, _U32, _Text, _Text, _Text, _Text, _U16, _U16, _Text,
    _RegisterReply, _Pointer,
]
_dns_sd.DNSServiceRegister.restype = _I32


class DnsSdRequest:
    """Owns one dns_sd request and delivers its replies on the event loop."""

    def __init__(self, start, reply):
        self._loop = asyncio.get_running_loop()
        self._reply = reply  # ctypes drops the callback if nothing retains it
        self._ref = _Ref()
        status = start(ctypes.byref(self._ref), reply)
        if status != 0:
            raise OSError(f"dns_sd request failed with status {status}")
        self._fd = _dns_sd.DNSServiceRefSockFD(self._ref)
        self._loop.add_reader(self._fd, _dns_sd.DNSServiceProcessResult, self._ref)

    def cancel(self):
        if self._ref:
            self._loop.remove_reader(self._fd)
            _dns_sd.DNSServiceRefDeallocate(self._ref)
            self._ref = None


# Relay


@dataclasses.dataclass
class Phone:
    name: str  # Rotating identifier, also the Bonjour instance name.
    interface_index: int
    port: int = 0
    txt: bytes = b""
    address: Optional[str] = None
    last_control: float = 0.0
    lookup: Optional[DnsSdRequest] = None
    advert: Optional[DnsSdRequest] = None
    control_server: Optional[asyncio.AbstractServer] = None

    @property
    def label(self) -> str:
        return self.name[:8]


def listening_socket(address: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
    except OSError:
        sock.close()
        raise
    sock.setblocking(False)
    return sock


async def connect_to_phone(phone: Phone, port: int):
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.sock_connect(sock, (phone.address, port, 0, phone.interface_index)),
            CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError):
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock, limit=BUFFER_BYTES)


async def copy_stream(reader, writer):
    try:
        while True:
            data = await reader.read(BUFFER_BYTES)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def splice(left, right):
    await asyncio.gather(
        copy_stream(left[0], right[1]), copy_stream(right[0], left[1])
    )


def is_link_peer(writer) -> bool:
    return writer.get_extra_info("peername")[0].startswith(LINK_PREFIX)


class Relay:
    def __init__(self):
        self.phones: Dict[Tuple[int, str], Phone] = {}
        self.link: Optional[Interface] = None
        self.tunnel_servers: List[asyncio.AbstractServer] = []
        self.last_phone_time = 0.0

    async def run(self):
        # Python 3.9 binds an Event to the loop current at construction.
        self.wake = asyncio.Event()
        resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))
        reply = _BrowseReply(self._on_browse)
        self.browser = DnsSdRequest(
            lambda ref, callback: _dns_sd.DNSServiceBrowse(
                ref, 0, 0, SERVICE_TYPE, b"local", callback, None
            ),
            reply,
        )
        log("started")
        while True:
            await self._reconcile()
            try:
                await asyncio.wait_for(self.wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    # Discovery

    def _on_browse(self, _ref, flags, interface_index, status, name, _type, _domain, _context):
        if status != 0:
            log("browse error", status)
            return
        key = (interface_index, name.decode())
        if flags & FLAG_ADD:
            self._phone_appeared(*key)
        elif key in self.phones:
            self._phone_disappeared(key)

    def _phone_appeared(self, interface_index, name):
        try:
            interface_name = socket.if_indextoname(interface_index)
        except OSError:
            return
        if not is_phone_interface(read_interfaces().get(interface_name)):
            return
        phone = Phone(name, interface_index)
        self.phones[(interface_index, name)] = phone
        log("phone", phone.label, "appeared on", interface_name)

        def on_resolved(_ref, _flags, _index, status, _full, host, port, txt_length, txt, _context):
            if status != 0:
                log("resolve error", status, "for", phone.label)
                return
            phone.port = socket.ntohs(port)
            phone.txt = ctypes.string_at(txt, txt_length)
            self._replace_lookup(phone, self._address_request(phone, host))

        reply = _ResolveReply(on_resolved)
        phone.lookup = DnsSdRequest(
            lambda ref, callback: _dns_sd.DNSServiceResolve(
                ref, 0, interface_index, name.encode(), SERVICE_TYPE, b"local",
                callback, None,
            ),
            reply,
        )

    def _address_request(self, phone: Phone, host: bytes) -> DnsSdRequest:
        def on_address(_ref, flags, _index, status, _host, address, _ttl, _context):
            if status != 0 or not flags & FLAG_ADD or not address:
                return
            raw = ctypes.string_at(address, 28)
            if raw[1] != socket.AF_INET6:
                return
            text = socket.inet_ntop(socket.AF_INET6, raw[8:24])
            if text.startswith("fe80"):
                phone.address = text
                log("phone", phone.label, "at", text, "port", phone.port)
                self._replace_lookup(phone, None)
                self.wake.set()

        return DnsSdRequest(
            lambda ref, callback: _dns_sd.DNSServiceGetAddrInfo(
                ref, 0, phone.interface_index, PROTOCOL_IPV6, host, callback, None
            ),
            _AddressReply(on_address),
        )

    def _replace_lookup(self, phone: Phone, lookup: Optional[DnsSdRequest]):
        # A request cannot be torn down from inside its own reply.
        previous, phone.lookup = phone.lookup, lookup
        if previous is not None:
            asyncio.get_running_loop().call_soon(previous.cancel)

    def _phone_disappeared(self, key):
        phone = self.phones.pop(key)
        log("phone", phone.label, "disappeared")
        self._replace_lookup(phone, None)
        self._unpublish(phone)
        self.wake.set()

    # Serving

    async def _reconcile(self):
        link = pick_link(read_interfaces())
        ready = [phone for phone in self.phones.values() if phone.address]
        now = time.monotonic()
        if ready:
            self.last_phone_time = now
        link_changed = self.link is not None and (
            link is None
            or (link.name, link.ipv4) != (self.link.name, self.link.ipv4)
        )
        idle = now - self.last_phone_time > IDLE_TEARDOWN_SECONDS
        if self.tunnel_servers and (link_changed or idle):
            self._stop_serving()
        if link is None or not ready:
            return
        if not self.tunnel_servers:
            await self._start_tunnel_listeners(link)
        for phone in ready:
            if phone.advert is None:
                await self._publish(phone)

    async def _start_tunnel_listeners(self, link: Interface):
        self.link = link
        for port in TUNNEL_PORTS:
            try:
                sock = listening_socket(link.ipv4, port)
            except OSError:
                continue
            handler = lambda r, w, port=port: self._handle_tunnel(port, r, w)
            self.tunnel_servers.append(
                await asyncio.start_server(handler, sock=sock, limit=BUFFER_BYTES)
            )
        log("listening on", link.name, link.ipv4, "ports", len(self.tunnel_servers))

    def _stop_serving(self):
        for phone in self.phones.values():
            self._unpublish(phone)
        for server in self.tunnel_servers:
            server.close()
        self.tunnel_servers = []
        self.link = None
        log("stopped listening")

    async def _publish(self, phone: Phone):
        for port in itertools.count(CONTROL_PORT_BASE):
            try:
                sock = listening_socket(self.link.ipv4, port)
                break
            except OSError:
                continue
        handler = lambda r, w: self._handle_control(phone, r, w)
        phone.control_server = await asyncio.start_server(
            handler, sock=sock, limit=BUFFER_BYTES
        )

        def on_registered(_ref, _flags, status, _name, _type, _domain, _context):
            if status != 0:
                log("advert error", status, "for", phone.label)

        link_index = socket.if_nametoindex(self.link.name)
        phone.advert = DnsSdRequest(
            lambda ref, callback: _dns_sd.DNSServiceRegister(
                ref, 0, link_index, phone.name.encode(), SERVICE_TYPE, b"local",
                None, socket.htons(port), len(phone.txt), phone.txt, callback, None,
            ),
            _RegisterReply(on_registered),
        )
        log("phone", phone.label, "published on", self.link.name, "port", port)

    def _unpublish(self, phone: Phone):
        if phone.advert is not None:
            phone.advert.cancel()
            phone.advert = None
        if phone.control_server is not None:
            phone.control_server.close()
            phone.control_server = None

    async def _handle_control(self, phone: Phone, reader, writer):
        if not is_link_peer(writer):
            writer.close()
            return
        phone.last_control = time.monotonic()
        try:
            upstream = await connect_to_phone(phone, phone.port)
        except (OSError, asyncio.TimeoutError) as error:
            log("phone", phone.label, "control connect failed:", repr(error))
            writer.close()
            return
        log("phone", phone.label, "control connected")
        await splice((reader, writer), upstream)

    async def _handle_tunnel(self, port: int, reader, writer):
        """Find the phone owning this port, trying the latest handshake first.

        Only the phone that allocated the listener accepts, so this also keeps
        several phones apart on one address without knowing their ports.
        """
        if not is_link_peer(writer):
            writer.close()
            return
        ready = [phone for phone in self.phones.values() if phone.address]
        for phone in sorted(ready, key=lambda p: p.last_control, reverse=True):
            try:
                upstream = await connect_to_phone(phone, port)
            except (OSError, asyncio.TimeoutError):
                continue
            log("phone", phone.label, "tunnel connected on port", port)
            await splice((reader, writer), upstream)
            log("phone", phone.label, "tunnel closed on port", port)
            return
        log("no phone accepted port", port)
        writer.close()


if __name__ == "__main__":
    asyncio.run(Relay().run())
