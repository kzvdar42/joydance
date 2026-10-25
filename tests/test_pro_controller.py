import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from joydance.constants import Command
from joydance.controllers.base import PRODUCT_ID_TO_CONTROLLER_CLASS
from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.pycon import ButtonEventJoyCon
from joydance.controllers.joycon.pycon.constants import (
    JOYCON_L_PRODUCT_ID,
    JOYCON_R_PRODUCT_ID,
    JOYCON_PRO_PRODUCT_ID,
    JOYCON_VENDOR_ID,
)


def pack_axes(x, y):
    return bytes((x & 0xFF, (x >> 8) | ((y & 0xF) << 4), y >> 4))


def calibration_data(side, values):
    if side == "right":
        values = values[2:] + values[:2]
    return b"".join(pack_axes(*values[index : index + 2]) for index in (0, 2, 4))


class ProControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = JoyConWrapper(
            JOYCON_VENDOR_ID, JOYCON_PRO_PRODUCT_ID, "pro-test"
        )
        self.joycon = self.controller.joycon

    def press(self, button, commands_to_check=None):
        with patch.object(self.joycon, "events", return_value=iter([(button, 1)])):
            return self.controller.get_latest_command(commands_to_check)

    def test_pro_is_discovered_and_tracks_both_button_banks(self):
        self.assertIs(
            PRODUCT_ID_TO_CONTROLLER_CLASS[JOYCON_PRO_PRODUCT_ID], JoyConWrapper
        )
        report = bytearray(49)
        report[3] = 0xCA  # X, A, R, ZR
        report[4] = 0x03  # Minus, Plus
        report[5] = 0xC2  # Up, L, ZL
        self.joycon._input_report = report
        for hook in self.joycon._input_hooks:
            hook(self.joycon)
        buttons = {"x", "a", "r", "zr", "minus", "plus", "up", "l", "zl"}
        self.assertEqual(set(self.joycon.events()), {(button, 1) for button in buttons})
        self.joycon._input_report = bytes(49)
        for hook in self.joycon._input_hooks:
            hook(self.joycon)
        self.assertEqual(set(self.joycon.events()), {(button, 0) for button in buttons})

    def test_pro_dpad_face_buttons_and_pause_filter(self):
        self.controller.available_shortcuts = {
            Command.ACTIVATE_DANCER_PROFILE,
            Command.SHORTCUT_EXTRA,
        }
        for button, command in (
            ("up", Command.UP),
            ("right", Command.RIGHT),
            ("down", Command.DOWN),
            ("left", Command.LEFT),
            ("a", Command.ACCEPT),
            ("b", Command.BACK),
            ("x", Command.ACTIVATE_DANCER_PROFILE),
            ("y", Command.SHORTCUT_EXTRA),
            ("minus", Command.BACK),
            ("plus", Command.PAUSE),
        ):
            with self.subTest(button=button):
                self.assertEqual(self.press(button), command)
                self.assertEqual(
                    self.press(button, {Command.PAUSE}),
                    Command.PAUSE if button == "plus" else None,
                )
        self.assertIsNone(self.press("up", set()))

    def test_pro_shoulders_select_previous_and_next_shortcuts(self):
        self.controller.available_shortcuts = {Command.TIPS_PREVIOUS, Command.TIPS_NEXT}
        for button in ("l", "zl"):
            self.assertEqual(self.press(button), Command.TIPS_PREVIOUS)
        for button in ("r", "zr"):
            self.assertEqual(self.press(button), Command.TIPS_NEXT)
        self.controller.available_shortcuts = {Command.NEXT_ACTION}
        for button in ("r", "zr"):
            self.assertEqual(self.press(button), Command.NEXT_ACTION)
        self.assertIsNone(self.press("l"))

    def test_both_pro_sticks_use_their_own_calibration_and_deadzone(self):
        self.joycon.stick_cal = {
            "left": [1000, 800, 2000, 2100, 900, 700],
            "right": [600, 900, 1900, 2200, 800, 1000],
        }
        self.joycon.deadzone = {"left": 100, "right": 150}
        report = bytearray(49)
        report[6:9] = pack_axes(2050, 2100)
        report[9:12] = pack_axes(1900, 1300)
        self.joycon._input_report = report
        sticks = self.joycon.get_status()["analog-sticks"]
        self.assertEqual(sticks["left"], {"horizontal": 0, "vertical": 0})
        self.assertAlmostEqual(sticks["right"]["vertical"], -0.9)
        self.assertEqual(self.controller.get_joystick_command(), Command.DOWN)
        report[6:9] = pack_axes(2800, 2100)
        self.assertAlmostEqual(self.joycon.get_stick_left_horizontal(), 0.8)
        self.assertEqual(self.controller.get_joystick_command(), Command.RIGHT)

    def test_user_and_factory_calibration_are_read_for_each_pro_stick(self):
        values = {
            "left": [1100, 900, 2000, 2100, 950, 850],
            "right": [800, 1000, 1900, 2200, 750, 950],
        }

        def spi_read(address, size):
            if address == 0x8010:
                return b"\xb2\xa1"
            if address in (0x801B, 0x8026):
                return b"\xff\xff"
            if address == 0x8012:
                return calibration_data("left", values["left"])
            if address == 0x6046:
                return calibration_data("right", values["right"])
            if address in (0x6086, 0x6098):
                return (
                    bytes(3)
                    + pack_axes(120 if address == 0x6086 else 180, 0)
                    + bytes(10)
                )
            if address == 0x6020:
                return bytes(6) + b"\x00\x40" * 3 + bytes(12)
            if address == 0x6050:
                self.assertEqual(size, 12)
                return bytes((30, 40, 50, 255, 255, 255, 60, 70, 80, 90, 100, 110))
            raise AssertionError(f"Unexpected SPI read: {address:#x}")

        with patch.object(self.joycon, "_spi_flash_read", side_effect=spi_read) as spi:
            self.joycon._read_joycon_data()
        self.assertEqual(self.joycon.stick_cal, values)
        self.assertEqual(self.joycon.deadzone, {"left": 120, "right": 180})
        self.assertNotIn(call(0x603D, 9), spi.call_args_list)
        self.assertEqual(self.joycon.color_body, (30, 40, 50))
        self.assertEqual(self.joycon.color_btn, (255, 255, 255))
        self.assertEqual(self.joycon.color_grip_left, (60, 70, 80))
        self.assertEqual(self.joycon.color_grip_right, (90, 100, 110))

    def test_invalid_calibration_keeps_usable_defaults(self):
        for invalid in (bytes(9), b"\xff" * 9, bytes(3)):
            with self.subTest(data=invalid):
                with patch.object(self.joycon, "_spi_flash_read", return_value=invalid):
                    with self.assertLogs(
                        "joydance.controllers.joycon.pycon.joycon", level="WARNING"
                    ):
                        self.joycon._read_joycon_data()
                for side, values in self.joycon.stick_cal.items():
                    self.assertTrue(all(value > 0 for value in values))
                    self.assertEqual(
                        self.joycon.get_actual_stick_value(values[2], 0, side), 0
                    )

    def test_joycons_keep_one_button_bank_and_one_active_stick(self):
        for product_id in (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID):
            with self.subTest(product_id=product_id):
                joycon = ButtonEventJoyCon(JOYCON_VENDOR_ID, product_id, "joycon-test")
                self.assertEqual(len(joycon._input_hooks), 1)
                side = "left" if joycon.is_left() else "right"
                report = bytearray(49)
                report[6:9] = report[9:12] = pack_axes(2000, 2000)
                joycon._input_report = report
                self.assertEqual(
                    joycon.get_status()["analog-sticks"][
                        "right" if side == "left" else "left"
                    ],
                    {"horizontal": 0, "vertical": 0},
                )


class ProControllerStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_assignment_and_disconnect_flash_twice_and_leave_the_correct_pattern(self):
        for player_id, pattern in ((0, 8), (1, 1), (2, 3), (3, 7), (4, 15), (5, 9), (6, 5), (7, 13), (8, 6)):
            with self.subTest(player_id=player_id):
                controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "led-test")
                self.addCleanup(controller.close)
                controller._is_battery_level_initialized = True
                with (
                    patch.object(controller.joycon, "set_player_lamp") as lamp,
                    patch("joydance.controllers.joycon.joycon_wrapper.asyncio.sleep", new=AsyncMock()) as sleep,
                ):
                    await controller.set_player_led(player_id, flash=True)
                    if player_id == 0:
                        lamp.assert_not_called()
                    else:
                        lamp.assert_called_once_with(pattern)
                    await controller._led_transition_task
                    transition = [call(n) for n in (1, 2, 4, 8)] if player_id == 0 else [call(pattern)]
                    self.assertEqual(lamp.call_args_list, transition + [call(0), call(pattern)] * 2)
                    movement_delays = [call(0.15)] * 4 if player_id == 0 else []
                    self.assertEqual(sleep.await_args_list, movement_delays + [call(0.35)] * 4)

    async def test_disconnect_or_close_cancels_pending_player_flash(self):
        for action in ("disconnect", "close"):
            with self.subTest(action=action):
                controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "led-test")
                self.addCleanup(controller.close)
                controller._is_battery_level_initialized = True
                turned_off = asyncio.Event()
                sleeps = 0

                async def pause(delay):
                    nonlocal sleeps
                    sleeps += 1
                    if sleeps == 2:
                        turned_off.set()
                        await asyncio.Event().wait()

                with (
                    patch.object(controller.joycon, "set_player_lamp") as lamp,
                    patch("joydance.controllers.joycon.joycon_wrapper.asyncio.sleep", side_effect=pause),
                ):
                    await controller.set_player_led(1, flash=True)
                    task = controller._led_transition_task
                    await asyncio.wait_for(turned_off.wait(), 1)
                    if action == "disconnect":
                        await controller.set_player_led(0, flash=True)
                    else:
                        controller.close()
                    await asyncio.gather(task, return_exceptions=True)
                    self.assertTrue(task.cancelled())
                    expected = [call(1), call(0)]
                    if action == "disconnect":
                        await controller._led_transition_task
                        expected.extend(call(n) for n in (1, 2, 4, 8, 0, 8, 0, 8))
                    self.assertEqual(lamp.call_args_list, expected)

    async def test_connecting_led_moves_one_dot_across_all_four_lights(self):
        for product_id in (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID):
            with self.subTest(product_id=product_id):
                controller = JoyConWrapper(JOYCON_VENDOR_ID, product_id, "led-test")
                self.addCleanup(controller.close)
                controller._is_battery_level_initialized = True
                with (
                    patch.object(controller.joycon, "set_player_lamp") as lamp,
                    patch(
                        "joydance.controllers.joycon.joycon_wrapper.asyncio.sleep",
                        new=AsyncMock(side_effect=[None] * 4 + [asyncio.CancelledError()]),
                    ) as sleep,
                ):
                    await controller.set_player_led(0, flash=True)
                    disconnect_flash = controller._led_transition_task
                    with self.assertRaises(asyncio.CancelledError):
                        await controller.animate_connecting_led()
                    await asyncio.gather(disconnect_flash, return_exceptions=True)
                    self.assertTrue(disconnect_flash.cancelled())
                self.assertEqual(lamp.call_args_list, [call(n) for n in (1, 2, 4, 8, 1)])
                self.assertEqual(sleep.await_args_list, [call(0.15)] * 5)

    async def test_name_and_icon_type_before_and_after_device_setup(self):
        for product_id, name, controller_type in (
            (JOYCON_PRO_PRODUCT_ID, "Pro Controller", "pro_controller"),
            (JOYCON_L_PRODUCT_ID, "JoyCon L", "joycon_left"),
            (JOYCON_R_PRODUCT_ID, "JoyCon R", "joycon_right"),
        ):
            with self.subTest(product_id=product_id):
                controller = JoyConWrapper(JOYCON_VENDOR_ID, product_id, "state-test")
                controller.battery_level = AsyncMock(return_value=4)
                initial = await controller.get_state(pull_new_data=False)
                self.assertEqual(initial["controller_type"], controller_type)
                self.assertNotIn("is_pro", initial)
                self.assertNotIn("is_left", initial)
                if controller.is_pro():
                    self.assertEqual(initial["name"], name)
                controller.joycon._was_state_initialized = True
                controller.joycon.color_body = (30, 40, 50)
                controller.joycon.color_btn = (60, 70, 80)
                controller.joycon.color_grip_left = (90, 100, 110)
                controller.joycon.color_grip_right = (120, 130, 140)
                state = await controller.get_state(pull_new_data=False)
                self.assertEqual(state["name"], name)
                self.assertEqual(state["color"], "#1e2832")
                self.assertEqual(state["color_buttons"], "#3c4650")
                self.assertEqual(state["controller_type"], controller_type)
                self.assertNotIn("is_pro", state)
                self.assertNotIn("is_left", state)
                if controller.is_pro():
                    self.assertEqual(state["color_grip_left"], "#5a646e")
                    self.assertEqual(state["color_grip_right"], "#78828c")

    async def test_missing_grip_colors_fall_back_to_body_color(self):
        for grips in (b"\xff" * 6, b""):
            with self.subTest(grips=grips):
                controller = JoyConWrapper(
                    JOYCON_VENDOR_ID, JOYCON_PRO_PRODUCT_ID, "color-test"
                )
                with patch.object(controller.joycon, "_spi_flash_read") as spi:
                    spi.side_effect = lambda address, size: (
                        bytes((30, 40, 50, 60, 70, 80)) + grips
                        if address == 0x6050
                        else bytes(size)
                    )
                    with self.assertLogs(
                        "joydance.controllers.joycon.pycon.joycon", level="WARNING"
                    ):
                        controller.joycon._read_joycon_data()
                controller.joycon._was_state_initialized = True
                controller.battery_level = AsyncMock(return_value=4)
                state = await controller.get_state(pull_new_data=False)
                self.assertEqual(state["color"], "#1e2832")
                self.assertEqual(state["color_buttons"], "#3c4650")
                self.assertEqual(state["color_grip_left"], "#1e2832")
                self.assertEqual(state["color_grip_right"], "#1e2832")


if __name__ == "__main__":
    unittest.main()
