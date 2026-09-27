import unittest

from scroll_forwarder import AxisFrame, WheelNormalizer, parse_args


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
        self.assertEqual(args.device, "/dev/input/event7")
        self.assertTrue(args.allow_unfocused)
        self.assertTrue(args.wait_for_device)


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
