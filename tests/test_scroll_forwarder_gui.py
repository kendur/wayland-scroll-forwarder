"""Tests for the GUI's pure logic: device discovery, rule round-trips, options.

No widgets are built here - the parts that matter for correctness are the ones
that decide what goes into a udev rule and what the service is told to do.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import scroll_forwarder_gui as gui
except ImportError as exc:  # pragma: no cover - only on a host without GTK
    gui = None
    IMPORT_ERROR = exc


@unittest.skipIf(gui is None, "python3-gobject (GTK 4 + libadwaita) is not installed")
class ProcDeviceTests(unittest.TestCase):
    # A receiver whose wheel sits on the keyboard interface, the exact shape a
    # gaming keypad has, plus a keyboard with no wheel at all.
    SAMPLE = """\
I: Bus=0003 Vendor=046d Product=c547 Version=0111
N: Name="Logitech USB Receiver Keyboard"
P: Phys=usb-0000:04:00.3-1.2.4/input1
H: Handlers=sysrq kbd event12 leds
B: EV=120013
B: REL=1940

I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
H: Handlers=sysrq kbd event3 leds
B: EV=120013

I: Bus=0005 Vendor=046d Product=b034 Version=0012
N: Name="Logitech Wireless Mouse MX Master 3"
H: Handlers=mouse3 event19 event20
B: EV=17
B: REL=1943

"""

    def test_every_event_node_is_listed(self):
        interfaces = gui.parse_proc_devices(self.SAMPLE)
        self.assertEqual(
            [interface.node for interface in interfaces],
            ["/dev/input/event12", "/dev/input/event3", "/dev/input/event19", "/dev/input/event20"],
        )

    def test_wheel_is_detected_on_a_keyboard_interface(self):
        interfaces = {interface.node: interface for interface in gui.parse_proc_devices(self.SAMPLE)}
        self.assertTrue(interfaces["/dev/input/event12"].wheel)
        self.assertFalse(interfaces["/dev/input/event3"].wheel)

    def test_non_event_handlers_are_ignored(self):
        nodes = [interface.node for interface in gui.parse_proc_devices(self.SAMPLE)]
        self.assertNotIn("/dev/input/mouse3", nodes)

    def test_multi_word_bitmask_is_recombined_least_significant_word_last(self):
        # REL_WHEEL is bit 8; put it in the low word and check the high word
        # does not shift it away.
        self.assertTrue(gui._mask_value("0 100") >> 8 & 1)
        self.assertFalse(gui._mask_value("100 0") >> 8 & 1)


@unittest.skipIf(gui is None, "python3-gobject (GTK 4 + libadwaita) is not installed")
class RuleTests(unittest.TestCase):
    RULE = """\
# generated header
ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_MOUSE}=="1", ATTRS{name}=="Naga V2 Pro Mouse", TAG+="uaccess", SYMLINK+="input/wsf/%k"
ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_KEY}=="1", ATTRS{name}=="Razer Razer Tartarus V2", TAG+="uaccess", SYMLINK+="input/wsf/%k"
ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Odd Device", TAG+="uaccess", SYMLINK+="input/wsf/%k"
"""

    def test_kinds_survive_the_round_trip(self):
        self.assertEqual(
            gui.parse_rule(self.RULE),
            [("mouse", "Naga V2 Pro Mouse"), ("key", "Razer Razer Tartarus V2"), ("any", "Odd Device")],
        )

    def test_comments_are_skipped(self):
        self.assertEqual(gui.parse_rule("# ATTRS{name}==\"Not A Rule\"\n"), [])

    def test_spec_matches_what_the_installer_parses(self):
        group = gui.DeviceGroup(name="Razer Razer Tartarus V2", kind="key")
        self.assertEqual(group.spec, "key:Razer Razer Tartarus V2")


@unittest.skipIf(gui is None, "python3-gobject (GTK 4 + libadwaita) is not installed")
class PlausibilityTests(unittest.TestCase):
    def test_wheel_device_is_always_offered(self):
        self.assertTrue(gui.DeviceGroup(name="x", kind="other", wheel=True).plausible)

    def test_already_selected_device_is_always_offered(self):
        # Otherwise applying any change would quietly drop a sleeping mouse.
        self.assertTrue(gui.DeviceGroup(name="x", kind="other", selected=True).plausible)

    def test_peripheral_buses_are_offered(self):
        self.assertTrue(gui.DeviceGroup(name="x", kind="key", bus="usb").plausible)
        self.assertTrue(gui.DeviceGroup(name="x", kind="mouse", bus="bluetooth").plausible)

    def test_built_in_switches_are_hidden(self):
        self.assertFalse(gui.DeviceGroup(name="Lid Switch", kind="other").plausible)
        self.assertFalse(gui.DeviceGroup(name="AT keyboard", kind="key", bus="").plausible)


@unittest.skipIf(gui is None, "python3-gobject (GTK 4 + libadwaita) is not installed")
class OptionsTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forwarder.env"
            with mock.patch.object(gui, "ENV_FILE", path):
                gui.Options(window_class="Steam", allow_unfocused=False, wait_for_device=True).save()
                loaded = gui.Options.load()
        self.assertEqual(loaded.window_class, "Steam")
        self.assertFalse(loaded.allow_unfocused)
        self.assertTrue(loaded.wait_for_device)

    def test_defaults_when_no_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(gui, "ENV_FILE", Path(directory) / "missing.env"):
                loaded = gui.Options.load()
        self.assertEqual(loaded.window_class, gui.DEFAULT_WINDOW_CLASS)

    def test_window_class_must_be_a_single_token(self):
        # It is expanded unquoted in ExecStart, so a space would become a second
        # argument rather than a longer class name.
        self.assertTrue(gui.valid_window_class("GeForceNOW"))
        self.assertFalse(gui.valid_window_class("two words"))
        self.assertFalse(gui.valid_window_class(""))
        self.assertFalse(gui.valid_window_class("semi;colon"))


if __name__ == "__main__":
    unittest.main()


INSTALLER = Path(__file__).resolve().parent.parent / "install-persistent.sh"


@unittest.skipUnless(INSTALLER.is_file(), "install-persistent.sh is not next to the tests")
class RuleGenerationTests(unittest.TestCase):
    """The installer owns rule generation; check it from the outside.

    --print-rule changes nothing, so this needs no root and no fixtures.
    """

    def generate(self, *specs: str) -> str:
        import subprocess

        result = subprocess.run(
            [str(INSTALLER), "--print-rule", "--", *specs],
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def test_order_and_duplicates_do_not_change_the_rule(self):
        # Otherwise merely reordering a selection would rewrite /etc and ask for
        # a password for a change that is not one.
        first = self.generate("Naga V2 Pro Mouse", "key:Razer Razer Tartarus V2", "Logitech USB Receiver")
        second = self.generate("Logitech USB Receiver", "Naga V2 Pro Mouse",
                               "key:Razer Razer Tartarus V2", "Naga V2 Pro Mouse")
        self.assertEqual(first, second)

    def test_interface_class_reaches_the_rule(self):
        rule = self.generate("mouse:A Mouse", "key:A Keypad", "any:A Thing")
        self.assertIn('ENV{ID_INPUT_MOUSE}=="1", ATTRS{name}=="A Mouse"', rule)
        self.assertIn('ENV{ID_INPUT_KEY}=="1", ATTRS{name}=="A Keypad"', rule)
        self.assertIn('KERNEL=="event*", ATTRS{name}=="A Thing"', rule)

    @unittest.skipIf(gui is None, "python3-gobject (GTK 4 + libadwaita) is not installed")
    def test_rule_survives_a_round_trip_through_the_parser(self):
        rule = self.generate("mouse:A Mouse", "key:A Keypad")
        self.assertEqual(gui.parse_rule(rule), [("key", "A Keypad"), ("mouse", "A Mouse")])

    def test_names_that_could_inject_a_match_key_are_rejected(self):
        import subprocess

        for hostile in ('evil", TAG+="uaccess', "evil\nACTION", "evil$(id)", "../evil"):
            with self.subTest(name=hostile):
                result = subprocess.run(
                    [str(INSTALLER), "--print-rule", "--", hostile],
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
