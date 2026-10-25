import unittest
from unittest.mock import Mock, patch

from joydance.controllers.joycon.pycon.constants import JOYCON_L_PRODUCT_ID, JOYCON_VENDOR_ID
from joydance.controllers.joycon.pycon.joycon import JoyCon


class RumbleEncodingTests(unittest.TestCase):
    def setUp(self):
        self.controller = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID)
        self.addCleanup(self.controller.close)

    def test_successful_and_padded_writes_do_not_warn(self):
        for returned in (None, 10, 49, 64):
            device = Mock(write=Mock(return_value=returned))
            self.controller._joycon_device = device
            with (
                self.subTest(returned=returned),
                patch("joydance.controllers.joycon.pycon.joycon.logger.warning") as warning,
            ):
                self.controller.stop_rumble()
            warning.assert_not_called()
            device.error.assert_not_called()
            self.assertEqual(len(device.write.call_args.args[0]), 10)

    def test_failed_and_short_writes_still_warn(self):
        for command, returned in ((b"\x10", -1), (b"\x10", 0), (b"\x10", 9), (b"\x01", 48)):
            device = Mock(write=Mock(return_value=returned), error=Mock(return_value="mock HID error"))
            self.controller._joycon_device = device
            with (
                self.subTest(command=command, returned=returned),
                patch("joydance.controllers.joycon.pycon.joycon.logger.warning") as warning,
            ):
                self.controller._write_output_report(command, b"\x40", b"\x01")
            warning.assert_called_once()
            self.assertEqual(warning.call_args.args[2], returned)
            device.error.assert_called_once_with()



if __name__ == "__main__":
    unittest.main()
