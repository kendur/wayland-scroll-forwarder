import unittest
from unittest import mock

import scroll_forwarder
from scroll_forwarder import AxisFrame, WheelDeviceSet, WheelNormalizer, parse_args


class WheelNormalizerTests(unittest.TestCase):
    def test_legacy_event(self):
        normalizer = WheelNormalizer()
        self.assertEqual(normalizer.steps("vertical", AxisFrame(legacy=-2, saw_legacy=True)), -2)

    def test_paired_events_are_not_doubled(self):
        normalizer = WheelNormalizer()
        frame = AxisFrame(legacy=1, hi_res=120, saw_legacy=True, saw_hi_res=True)
        self.assertEqual(normalizer.steps("vertical", frame), 1)

    def test_high_resolution_events_accumulate(self):
        normalizer = WheelNormalizer()
        partial = AxisFrame(hi_res=40, saw_hi_res=True)
        self.assertEqual(normalizer.steps("vertical", partial), 0)
        self.assertEqual(normalizer.steps("vertical", partial), 0)
        self.assertEqual(normalizer.steps("vertical", partial), 1)

    def test_axes_have_independent_remainders(self):
        normalizer = WheelNormalizer()
        partial = AxisFrame(hi_res=-60, saw_hi_res=True)
        self.assertEqual(normalizer.steps("vertical", partial), 0)
        self.assertEqual(normalizer.steps("horizontal", partial), 0)
        self.assertEqual(normalizer.steps("vertical", partial), -1)
        self.assertEqual(normalizer.steps("horizontal", partial), -1)


class ArgumentTests(unittest.TestCase):
    def test_required_runtime_values_parse(self):
        args = parse_args(
            ["GeForceNOW", "--device", "/dev/input/event7", "--allow-unfocused", "--wait-for-device"]
        )
        self.assertEqual(args.window_class, "GeForceNOW")
        self.assertEqual(args.device, ["/dev/input/event7"])
        self.assertTrue(args.allow_unfocused)
        self.assertTrue(args.wait_for_device)

    def test_device_is_repeatable(self):
        args = parse_args(
            ["GeForceNOW", "--device", "/dev/input/event7", "--device", "/dev/input/event19"]
        )
        self.assertEqual(args.device, ["/dev/input/event7", "/dev/input/event19"])

    def test_device_dir_parses(self):
        args = parse_args(["GeForceNOW", "--device-dir", "/dev/input/wsf"])
        self.assertEqual(args.device_dir, "/dev/input/wsf")
        self.assertIsNone(args.device)


if __name__ == "__main__":
    unittest.main()


class XauthFallbackTests(unittest.TestCase):
    COOKIE = b"\x01\x02\x03"

    def _authority(self, entries):
        from Xlib import xauth

        authority = xauth.Xauthority.__new__(xauth.Xauthority)
        authority.entries = entries
        return authority

    def test_exact_hostname_match_still_preferred(self):
        from Xlib import xauth

        authority = self._authority(
            [
                (0xFFFF, b"", b"0", b"MIT-MAGIC-COOKIE-1", b"wild"),
                (xauth.FamilyLocal, b"bazzite", b"0", b"MIT-MAGIC-COOKIE-1", self.COOKIE),
            ]
        )
        self.assertEqual(
            authority.get_best_auth(xauth.FamilyLocal, b"bazzite", 0),
            (b"MIT-MAGIC-COOKIE-1", self.COOKIE),
        )

    def test_hostname_change_falls_back_to_wildcard(self):
        from Xlib import xauth

        authority = self._authority(
            [
                (xauth.FamilyLocal, b"bazzite", b"0", b"MIT-MAGIC-COOKIE-1", b"local"),
                (0xFFFF, b"", b"0", b"MIT-MAGIC-COOKIE-1", self.COOKIE),
            ]
        )
        self.assertEqual(
            authority.get_best_auth(xauth.FamilyLocal, b"803f5dd87f08", 0),
            (b"MIT-MAGIC-COOKIE-1", self.COOKIE),
        )

    def test_hostname_change_falls_back_to_other_local_entry(self):
        from Xlib import xauth

        authority = self._authority(
            [(xauth.FamilyLocal, b"bazzite", b"0", b"MIT-MAGIC-COOKIE-1", self.COOKIE)]
        )
        self.assertEqual(
            authority.get_best_auth(xauth.FamilyLocal, b"803f5dd87f08", 0),
            (b"MIT-MAGIC-COOKIE-1", self.COOKIE),
        )

    def test_other_display_number_is_not_used(self):
        from Xlib import error, xauth

        authority = self._authority(
            [(xauth.FamilyLocal, b"bazzite", b"1", b"MIT-MAGIC-COOKIE-1", self.COOKIE)]
        )
        with self.assertRaises(error.XNoAuthError):
            authority.get_best_auth(xauth.FamilyLocal, b"803f5dd87f08", 0)


class FakeDevice:
    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.closed = False

    def fileno(self):
        return hash(self.path) % 4096

    def close(self):
        self.closed = True


class WheelDeviceSetTests(unittest.TestCase):
    def setUp(self):
        self.available = {}
        patcher = mock.patch.object(
            scroll_forwarder, "validate_device_path", side_effect=lambda text: text
        )
        self.addCleanup(patcher.stop)
        patcher.start()

        def fake_open(text):
            try:
                return self.available[text]
            except KeyError as exc:
                raise OSError(f"no such device {text}") from exc

        opener = mock.patch.object(scroll_forwarder, "open_wheel_device", side_effect=fake_open)
        self.addCleanup(opener.stop)
        opener.start()

    def test_scan_opens_only_available_devices(self):
        self.available["/dev/input/event7"] = FakeDevice("/dev/input/event7", "Naga V2 Pro Mouse")
        devices = WheelDeviceSet(["/dev/input/event7", "/dev/input/event19"])
        opened = devices.scan(force=True)
        self.assertEqual([text for text, _ in opened], ["/dev/input/event7"])
        self.assertEqual(list(devices.open_devices), ["/dev/input/event7"])

    def test_second_mouse_is_picked_up_on_a_later_scan(self):
        first = FakeDevice("/dev/input/event7", "Naga V2 Pro Mouse")
        self.available["/dev/input/event7"] = first
        devices = WheelDeviceSet(["/dev/input/event7", "/dev/input/event19"])
        devices.scan(force=True)

        second = FakeDevice("/dev/input/event19", "Logitech Wireless Mouse MX Master 3")
        self.available["/dev/input/event19"] = second
        opened = devices.scan(force=True)
        self.assertEqual([device for _, device in opened], [second])
        self.assertEqual(len(devices.open_devices), 2)

    def test_already_open_devices_are_not_reopened(self):
        self.available["/dev/input/event7"] = FakeDevice("/dev/input/event7", "Naga V2 Pro Mouse")
        devices = WheelDeviceSet(["/dev/input/event7"])
        devices.scan(force=True)
        self.assertEqual(devices.scan(force=True), [])

    def test_drop_closes_device_and_allows_reopen(self):
        device = FakeDevice("/dev/input/event7", "Naga V2 Pro Mouse")
        self.available["/dev/input/event7"] = device
        devices = WheelDeviceSet(["/dev/input/event7"])
        devices.scan(force=True)

        devices.drop("/dev/input/event7")
        self.assertTrue(device.closed)
        self.assertEqual(devices.open_devices, {})

        replacement = FakeDevice("/dev/input/event7", "Naga V2 Pro Mouse")
        self.available["/dev/input/event7"] = replacement
        # drop() clears the backoff, so the woken mouse returns on the next scan.
        self.assertEqual([d for _, d in devices.scan()], [replacement])

    def test_scan_is_rate_limited_unless_forced(self):
        devices = WheelDeviceSet(["/dev/input/event7"])
        devices.scan(force=True)
        self.available["/dev/input/event7"] = FakeDevice("/dev/input/event7", "Naga V2 Pro Mouse")
        self.assertEqual(devices.scan(), [])

    def test_candidates_include_device_dir_entries(self):
        with mock.patch.object(scroll_forwarder, "Path") as fake_path:
            fake_path.return_value.iterdir.return_value = [
                "/dev/input/wsf/event19",
                "/dev/input/wsf/event7",
            ]
            devices = WheelDeviceSet(["/dev/input/event3"], "/dev/input/wsf")
        self.assertEqual(
            devices.candidates(),
            ["/dev/input/event3", "/dev/input/wsf/event19", "/dev/input/wsf/event7"],
        )

    def test_missing_device_dir_is_not_fatal(self):
        devices = WheelDeviceSet(None, "/dev/input/definitely-absent")
        self.assertEqual(devices.candidates(), [])
        self.assertEqual(devices.scan(force=True), [])


class PerDeviceNormalizerTests(unittest.TestCase):
    def test_two_mice_do_not_pool_hi_res_remainders(self):
        forwarder = scroll_forwarder.ScrollForwarder.__new__(scroll_forwarder.ScrollForwarder)
        forwarder.allow_unfocused = True
        forwarder.normalizers = {}
        forwarder.inject_steps = mock.Mock()

        from evdev import ecodes

        class Event:
            def __init__(self, code, value):
                self.type = ecodes.EV_REL
                self.code = code
                self.value = value

        half = [Event(ecodes.REL_WHEEL_HI_RES, 60)]
        # 60 units is half a step; the same half from each mouse must not add up.
        forwarder.process_report(half, "mouse-a")
        forwarder.process_report(half, "mouse-b")
        forwarder.inject_steps.assert_not_called()
        # A second half from one mouse completes that mouse's own step.
        forwarder.process_report(half, "mouse-a")
        forwarder.inject_steps.assert_called_once_with("vertical", 1)
