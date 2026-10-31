import unittest
from unittest.mock import Mock, patch

from joydance.controllers.joycon.pycon.constants import JOYCON_L_PRODUCT_ID, JOYCON_VENDOR_ID
from joydance.controllers.joycon.pycon.joycon import JoyCon


class RumbleEncodingTests(unittest.TestCase):
    def setUp(self):
        self.controller = JoyCon(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID)
        self.addCleanup(self.controller.close)

    def test_amplitude_vectors_from_reference_table(self):
        # https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering/blob/master/rumble_data_table.md#amplitude-table
        for amplitude, actuator in (
            (0.0, "00 01 40 40"),
            (0.007843, "00 03 e0 40"),
            (0.011823, "00 05 60 41"),
            (0.033442, "00 11 60 44"),
            (0.094592, "00 1d 60 47"),
            (0.112491, "00 1f e0 47"),
            (0.117471, "00 21 60 48"),
            (0.122671, "00 23 e0 48"),
            (0.224982, "00 3f e0 4f"),
            (0.229908, "00 41 60 50"),
            (0.501433, "00 89 60 62"),
            (0.981378, "00 c7 e0 71"),
            (1.0, "00 c9 60 72"),
        ):
            with self.subTest(amplitude=amplitude):
                self.assertEqual(self.controller.encode_rumble_data(320, amplitude), bytes.fromhex(actuator) * 2)

    def test_frequency_vectors_and_independent_band_limits(self):
        for frequency, actuator in (
            (-1, "04 c8 01 72"),
            (40.875885, "04 c8 01 72"),
            (160, "80 c8 40 72"),
            (320, "00 c9 60 72"),
            (626.286133, "7c c9 7f 72"),
            (1252.572266, "fc c9 7f 72"),
            (10000, "fc c9 7f 72"),
        ):
            with self.subTest(frequency=frequency):
                self.assertEqual(self.controller.encode_rumble_data(frequency, 1), bytes.fromhex(actuator) * 2)

    def test_zero_and_tiny_amplitudes_are_neutral_and_limits_are_clamped(self):
        for frequency in (41, 160, 320, 1253):
            for amplitude in (-1, 0, 0.001):
                self.assertEqual(self.controller.encode_rumble_data(frequency, amplitude), JoyCon._RUMBLE_DATA)
        self.assertEqual(self.controller.encode_rumble_data(320, 2), self.controller.encode_rumble_data(320, 1))

    def test_amplitude_sweep_is_monotonic_and_stays_inside_safe_wire_limits(self):
        previous = 0
        for step in range(10001):
            report = self.controller.encode_rumble_data(320, step / 10000)
            high_amplitude = (report[1] & 0xFE) >> 1
            low_amplitude = (report[3] - 0x40) * 2 + (report[2] >> 7)
            self.assertEqual(high_amplitude, low_amplitude)
            self.assertLessEqual(high_amplitude, 100)
            self.assertGreaterEqual(high_amplitude, previous)
            self.assertEqual(report[:4], report[4:])
            previous = high_amplitude

    def test_nonfinite_inputs_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                self.controller.encode_rumble_data(value, 0.3)
            with self.assertRaises(ValueError):
                self.controller.encode_rumble_data(320, value)

    def test_rumble_and_stop_send_correct_output_reports(self):
        device = Mock()
        device.write.side_effect = len
        self.controller._joycon_device = device
        self.controller.rumble(160, 0.3)
        self.controller.stop_rumble()
        self.assertEqual(device.write.call_args_list[0].args[0], bytes.fromhex("10 00 80 58 40 56 80 58 40 56"))
        self.assertEqual(device.write.call_args_list[1].args[0], b"\x10\x01" + JoyCon._RUMBLE_DATA)

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

    def test_two_band_rumble_encodes_bands_independently_and_respects_disabled_state(self):
        device = Mock(write=Mock(side_effect=len))
        self.controller._joycon_device = device
        low = self.controller.encode_rumble_data(200, .175)
        high = self.controller.encode_rumble_data(640, .65)
        self.controller.rumble_two_band(200, .175, 640, .65)
        self.assertEqual(device.write.call_args.args[0], b'\x10\x00' + (high[:2] + low[2:4]) * 2)
        self.controller.rumble_enabled = False
        self.assertEqual(device.write.call_args.args[0][2:], JoyCon._RUMBLE_DATA)
        device.write.reset_mock()
        self.controller.rumble_two_band(200, .175, 640, .65)
        device.write.assert_not_called()

    def test_failed_and_short_writes_raise(self):
        for command, returned in ((b"\x10", -1), (b"\x10", 0), (b"\x10", 9),
                                  (b"\x01", -1), (b"\x01", 0), (b"\x01", 48)):
            device = Mock(write=Mock(return_value=returned), error=Mock(return_value="mock HID error"))
            self.controller._joycon_device = device
            with (
                self.subTest(command=command, returned=returned),
                self.assertRaisesRegex(OSError, "mock HID error"),
            ):
                self.controller._write_output_report(command, b"\x40", b"\x01")
            device.error.assert_called_once_with()
            self.assertEqual(self.controller._packet_number, 0)


if __name__ == "__main__":
    unittest.main()
