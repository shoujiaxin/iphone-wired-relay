import re
import socket
import unittest

import iphone_bridge
from iphone_bridge import BRIDGE, THUNDERBOLT_BRIDGE, Interface

SAMPLE = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.119.136 netmask 0xffffff00 broadcast 192.168.119.255
\tstatus: active
en8: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tstatus: inactive
en9: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::3442:a0ff:fefc:ac97%en9 prefixlen 64 scopeid 0x1e
\tstatus: active
en11: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::44f2:36ff:febc:26cf%en11 prefixlen 64 scopeid 0x1c
\tstatus: active
en12: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1ca0:5c35:5496:9e27%en12 prefixlen 64 secured scopeid 0x1f
\tinet 169.254.228.187 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
"""

BRIDGE_SAMPLE = f"""\
{BRIDGE}: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tConfiguration:
\t\tid 0:0:0:0:0:0 priority 0 hellotime 0 fwddelay 0
\tmember: en12 flags=3<LEARNING,DISCOVER>
\t        ifmaxaddr 0 port 31 priority 0 path cost 0
\tmember: en11 flags=3<LEARNING,DISCOVER>
\t        ifmaxaddr 0 port 28 priority 0 path cost 0
\tstatus: active
"""

THUNDERBOLT_SAMPLE = """\
en2: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tstatus: active
\ttype: IP over Thunderbolt
en3: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tstatus: inactive
\ttype: IP over Thunderbolt
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 169.254.40.2 netmask 0xffff0000 broadcast 169.254.255.255
\tmember: en2 flags=3<LEARNING,DISCOVER>
\tmember: en3 flags=3<LEARNING,DISCOVER>
\tstatus: active
"""

BROWSE_ADD = (
    "17:00:10.302  Add        3  28 local.               "
    "_remotepairing._tcp. 353EE6C7-06AB-4A50-ABBD-226A64AD6802"
)
BROWSE_REMOVE = (
    "17:07:29.100  Rmv        0  28 local.               "
    "_remotepairing._tcp. 353EE6C7-06AB-4A50-ABBD-226A64AD6802"
)
BROWSE_KEY = (28, "353EE6C7-06AB-4A50-ABBD-226A64AD6802")


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = iphone_bridge.parse_ifconfig(SAMPLE)

    def test_reads_address_and_status(self):
        link = self.interfaces["en12"]
        self.assertEqual(link.ipv4, "169.254.228.187")
        self.assertTrue(link.active)
        self.assertFalse(self.interfaces["en8"].active)

    def test_does_not_treat_inet6_as_ipv4(self):
        self.assertIsNone(self.interfaces["en11"].ipv4)

    def test_reads_bridge_members(self):
        bridge = iphone_bridge.parse_ifconfig(BRIDGE_SAMPLE)[BRIDGE]
        self.assertEqual(bridge.members, ["en12", "en11"])

    def test_recognizes_thunderbolt_ports(self):
        interfaces = iphone_bridge.parse_ifconfig(THUNDERBOLT_SAMPLE)
        self.assertTrue(interfaces["en2"].thunderbolt)
        self.assertFalse(self.interfaces["en12"].thunderbolt)

    def test_picks_active_self_assigned_interface_as_link(self):
        self.assertEqual(iphone_bridge.pick_link(self.interfaces).name, "en12")

    def test_ignores_inactive_self_assigned_interface(self):
        self.interfaces["en12"].active = False
        self.assertIsNone(iphone_bridge.pick_link(self.interfaces))

    def test_never_picks_a_bridge_as_link(self):
        # A bridge cannot be a member of another bridge.
        self.interfaces["bridge0"] = Interface("bridge0", True, "169.254.1.1")
        self.assertEqual(iphone_bridge.pick_link(self.interfaces).name, "en12")

    def test_phone_interface_has_no_ipv4(self):
        self.assertTrue(iphone_bridge.is_phone_interface(self.interfaces["en11"]))
        self.assertFalse(iphone_bridge.is_phone_interface(self.interfaces["en1"]))
        self.assertFalse(iphone_bridge.is_phone_interface(self.interfaces["en12"]))
        self.assertFalse(iphone_bridge.is_phone_interface(None))


class BrowseTests(unittest.TestCase):
    def test_add_records_interface_and_instance(self):
        adverts = set()
        iphone_bridge.apply_browse_line(BROWSE_ADD, adverts)
        self.assertEqual(adverts, {BROWSE_KEY})

    def test_remove_forgets_advert(self):
        adverts = {BROWSE_KEY}
        iphone_bridge.apply_browse_line(BROWSE_REMOVE, adverts)
        self.assertEqual(adverts, set())

    def test_ignores_preamble(self):
        adverts = set()
        for line in (
            "Browsing for _remotepairing._tcp.local",
            "DATE: ---Wed 04 Nov 2026---",
            "17:06:47.821  ...STARTING...",
            "Timestamp     A/R    Flags  if Domain               "
            "Service Type         Instance Name",
        ):
            iphone_bridge.apply_browse_line(line, adverts)
        self.assertEqual(adverts, set())


class InterfaceNameTests(unittest.TestCase):
    def test_drops_adverts_of_interfaces_that_are_gone(self):
        # The interface can detach before dns-sd reports the advert as removed.
        adverts = {(socket.if_nametoindex("lo0"), "alive"), (9999, "gone")}
        self.assertEqual(iphone_bridge.interface_names(adverts), {"lo0"})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = iphone_bridge.parse_ifconfig(SAMPLE)

    def bridged(self, *members):
        self.interfaces[BRIDGE] = Interface(BRIDGE, True, None, list(members))

    def test_builds_bridge_from_nothing(self):
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [
                [BRIDGE, "create"],
                [BRIDGE, "addm", "en12"],
                [BRIDGE, "addm", "en11"],
                [BRIDGE, "up"],
            ],
        )

    def test_does_nothing_without_a_phone(self):
        self.assertEqual(iphone_bridge.plan(self.interfaces, set()), [])

    def test_does_nothing_without_a_link(self):
        self.interfaces["en12"].active = False
        self.assertEqual(iphone_bridge.plan(self.interfaces, {"en11"}), [])

    def test_does_nothing_when_up_to_date(self):
        self.bridged("en12", "en11")
        self.assertEqual(iphone_bridge.plan(self.interfaces, {"en11"}), [])

    def test_rejoins_a_replugged_phone(self):
        self.bridged("en12")
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [[BRIDGE, "addm", "en11"]],
        )

    def test_joins_a_link_that_came_back_under_a_new_name(self):
        self.bridged("en11")
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [[BRIDGE, "addm", "en12"]],
        )

    def test_skips_adverts_on_interfaces_with_ipv4(self):
        # Phones on Wi-Fi advertise on en1, which must never join the bridge.
        self.assertEqual(iphone_bridge.plan(self.interfaces, {"en1"}), [])

    def test_never_touches_the_private_link_without_an_advert(self):
        # en9 looks exactly like a phone interface but carries no advert.
        commands = iphone_bridge.plan(self.interfaces, {"en11"})
        self.assertNotIn([BRIDGE, "addm", "en9"], commands)


class ThunderboltPlanTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = iphone_bridge.parse_ifconfig(SAMPLE + THUNDERBOLT_SAMPLE)
        self.interfaces["en12"].active = False

    def bridged(self, *members):
        self.interfaces[BRIDGE] = Interface(BRIDGE, True, None, list(members))

    def join_thunderbolt(self, phone):
        self.interfaces[THUNDERBOLT_BRIDGE].members.append(phone)

    def test_phone_joins_the_system_bridge(self):
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [[THUNDERBOLT_BRIDGE, "addm", "en11"]],
        )

    def test_does_nothing_when_phone_is_already_in(self):
        self.join_thunderbolt("en11")
        self.assertEqual(iphone_bridge.plan(self.interfaces, {"en11"}), [])

    def test_does_nothing_without_a_phone(self):
        self.assertEqual(iphone_bridge.plan(self.interfaces, set()), [])

    def test_prefers_thunderbolt_when_both_cables_are_in(self):
        self.interfaces["en12"].active = True
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [[THUNDERBOLT_BRIDGE, "addm", "en11"]],
        )

    def test_moves_from_usb_to_thunderbolt(self):
        # A phone can belong to one bridge only, so the old one has to go first.
        self.bridged("en12", "en11")
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [[BRIDGE, "destroy"], [THUNDERBOLT_BRIDGE, "addm", "en11"]],
        )

    def test_moves_from_thunderbolt_to_usb(self):
        self.join_thunderbolt("en11")
        self.interfaces["en2"].active = False
        self.interfaces["en12"].active = True
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [
                [THUNDERBOLT_BRIDGE, "deletem", "en11"],
                [BRIDGE, "create"],
                [BRIDGE, "addm", "en12"],
                [BRIDGE, "addm", "en11"],
                [BRIDGE, "up"],
            ],
        )

    def test_leaves_the_system_bridge_when_no_cable_remains(self):
        self.join_thunderbolt("en11")
        self.interfaces["en2"].active = False
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"}),
            [[THUNDERBOLT_BRIDGE, "deletem", "en11"]],
        )

    def test_falls_back_to_usb_when_the_system_bridge_was_removed(self):
        del self.interfaces[THUNDERBOLT_BRIDGE]
        self.interfaces["en12"].active = True
        self.assertEqual(
            iphone_bridge.plan(self.interfaces, {"en11"})[0], [BRIDGE, "create"]
        )

    def test_never_changes_the_thunderbolt_ports(self):
        commands = iphone_bridge.plan(self.interfaces, {"en11"})
        self.assertFalse([c for c in commands if "en2" in c or "en3" in c])


class BridgeNameTests(unittest.TestCase):
    def test_unit_is_outside_what_configd_reconciles(self):
        # configd destroys bridges it does not know unless the unit has three
        # digits, so a phone replug would otherwise take the bridge down.
        self.assertRegex(BRIDGE, re.compile(r"^bridge[1-9][0-9]{2}$"))


if __name__ == "__main__":
    unittest.main()
