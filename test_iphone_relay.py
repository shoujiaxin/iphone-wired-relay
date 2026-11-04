import unittest

from iphone_relay import (
    Interface,
    TUNNEL_PORTS,
    is_phone_interface,
    parse_ifconfig,
    pick_link,
)

SAMPLE = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.119.136 netmask 0xffffff00 broadcast 192.168.119.255
\tstatus: active
en8: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tstatus: inactive
en11: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::44f2:36ff:febc:26cf%en11 prefixlen 64 scopeid 0x1c
\tstatus: active
en12: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1ca0:5c35:5496:9e27%en12 prefixlen 64 secured scopeid 0x1f
\tinet 169.254.228.187 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
"""


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = parse_ifconfig(SAMPLE)

    def test_reads_address_and_status(self):
        link = self.interfaces["en12"]
        self.assertEqual(link.ipv4, "169.254.228.187")
        self.assertTrue(link.active)
        self.assertFalse(self.interfaces["en8"].active)

    def test_does_not_treat_inet6_as_ipv4(self):
        self.assertIsNone(self.interfaces["en11"].ipv4)

    def test_picks_active_self_assigned_interface_as_link(self):
        self.assertEqual(pick_link(self.interfaces).name, "en12")

    def test_ignores_inactive_self_assigned_interface(self):
        self.interfaces["en12"].active = False
        self.assertIsNone(pick_link(self.interfaces))

    def test_prefers_thunderbolt_bridge(self):
        self.interfaces["bridge0"] = Interface("bridge0", True, "169.254.1.1")
        self.assertEqual(pick_link(self.interfaces).name, "bridge0")

    def test_phone_interface_has_no_ipv4(self):
        self.assertTrue(is_phone_interface(self.interfaces["en11"]))
        self.assertFalse(is_phone_interface(self.interfaces["en1"]))
        self.assertFalse(is_phone_interface(self.interfaces["en12"]))
        self.assertFalse(is_phone_interface(None))


class PortRangeTests(unittest.TestCase):
    def test_leaves_top_of_ephemeral_range_free(self):
        self.assertEqual(TUNNEL_PORTS[0], 49152)
        self.assertLessEqual(TUNNEL_PORTS[-1], 65535 - 1024)


if __name__ == "__main__":
    unittest.main()
