import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.protocol import State

from joydance.constants import Command
from joydance.controllers.joycon.pycon.constants import JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID
from joydance.games.justdance_v2 import JustDanceGameV2
from test_button_mapping import BufferedButtons, controller_with_buttons, input_collection, menu_setup


class TakeoverHoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = controller_with_buttons()
        self.controller.joycon = BufferedButtons()
        self.controller.is_connected = Mock(return_value=True)
        self.controller.get_joystick_command = Mock(return_value=None)
        self.game = JustDanceGameV2(
            self.controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock(),
        )
        self.command = {"__class": "JD_Input_PhoneCommandData", "input": 185785632}
        await self.game.on_message(json.dumps(menu_setup()))
        await self.game.on_message(json.dumps(input_collection({"VALID": self.command})))
        await self.message("JD_OpenTakeMasterScreen_ConsoleCommandData")

    async def message(self, name, **data):
        await self.game.on_message(json.dumps({"__class": name, **data}))

    async def test_metadata_only_ui_updates_preserve_hold_progress(self):
        self.set_button(True)
        self.assertIsNone(self.poll(0))
        await self.message("JD_PhoneUiSetupData", disabledSetupData={})
        await self.message("JD_PhoneUiSetupData", setupData=None, inputSetup=None)
        self.assertEqual(self.poll(2.01), Command.ACCEPT)

    def set_button(self, pressed, button="a"):
        if bool(getattr(self.controller.joycon, button, 0)) != pressed:
            setattr(self.controller.joycon, button, int(pressed))
            self.controller.joycon.joycon_button_event(button, int(pressed))

    def poll(self, now):
        commands = {cmd for cmd in Command if self.game.is_command_allowed(cmd)}
        with patch("joydance.games.justdance_v2.time", SimpleNamespace(monotonic=lambda: now)):
            return self.game._get_button_command(commands)

    async def test_short_taps_do_not_trigger_and_hold_sends_once_until_release(self):
        self.set_button(True)
        self.assertIsNone(self.poll(0))
        self.set_button(False)
        self.assertIsNone(self.poll(0.1))
        self.set_button(True)
        self.assertIsNone(self.poll(0.5))
        self.assertIsNone(self.poll(2.49))
        self.assertEqual(self.poll(2.51), Command.ACCEPT)
        self.assertIsNone(self.poll(5))
        self.set_button(False)
        self.assertIsNone(self.poll(5.1))
        self.set_button(True)
        self.assertIsNone(self.poll(5.2))
        self.assertEqual(self.poll(7.21), Command.ACCEPT)

    async def test_button_already_held_when_takeover_opens_requires_a_new_press(self):
        self.set_button(True)
        await self.message("JD_OpenTakeMasterScreen_ConsoleCommandData")
        self.assertIsNone(self.poll(0))
        self.assertIsNone(self.poll(3))
        self.set_button(False)
        self.assertIsNone(self.poll(3.1))
        self.set_button(True)
        self.assertIsNone(self.poll(3.2))
        self.assertEqual(self.poll(5.21), Command.ACCEPT)

    async def test_input_and_mapping_updates_cancel_progress_and_require_a_fresh_press(self):
        for message in (
            {"__class": "InputSetup_ConsoleCommandData", "isEnabled": True},
            {"__class": "EnableCarousel_ConsoleCommandData", "isEnabled": True},
            menu_setup(), input_collection({"VALID": self.command}),
        ):
            with self.subTest(message=message["__class"]):
                self.set_button(False)
                self.poll(0)
                self.set_button(True)
                self.assertIsNone(self.poll(1))
                await self.game.on_message(json.dumps(message))
                self.assertIsNone(self.poll(4))
                self.set_button(False)
                self.poll(4.1)
                self.set_button(True)
                self.assertIsNone(self.poll(4.2))
                self.assertEqual(self.poll(6.21), Command.ACCEPT)

    async def test_disabled_input_or_controller_loss_cancels_hold(self):
        for disabled in ("input", "controller"):
            with self.subTest(disabled=disabled):
                self.set_button(False)
                self.poll(0)
                self.set_button(True)
                self.assertIsNone(self.poll(1))
                if disabled == "input":
                    await self.message("InputSetup_ConsoleCommandData", isEnabled=False)
                else:
                    self.controller.is_connected.return_value = False
                self.assertIsNone(self.poll(4))
                await self.message("InputSetup_ConsoleCommandData", isEnabled=True)
                self.controller.is_connected.return_value = True
                self.assertIsNone(self.poll(7))

    async def test_popup_close_restores_immediate_tap_confirmation(self):
        self.set_button(True)
        self.assertIsNone(self.poll(0))
        await self.message("JD_ClosePopup_ConsoleCommandData")
        self.assertFalse(self.game._takeover_hold_enabled)
        self.set_button(False)
        self.poll(0.1)
        self.set_button(True)
        self.assertEqual(self.poll(0.2), Command.ACCEPT)

    async def test_disconnect_resets_hold_mode_and_progress(self):
        self.set_button(True)
        self.assertIsNone(self.poll(0))
        self.controller.set_accel_collection = Mock()
        self.controller.set_player_led = AsyncMock()
        await self.game.disconnect(should_reconnect=False)
        self.assertFalse(self.game._takeover_hold_enabled)
        self.assertIsNone(self.game._confirm_hold_started_at)
        await self.game.on_message(json.dumps(menu_setup()))
        self.set_button(False)
        self.poll(0.1)
        self.set_button(True)
        self.assertEqual(self.poll(0.2), Command.ACCEPT)

    async def test_takeover_requires_a_supplied_confirm_mapping(self):
        for mappings in (None, {}):
            with self.subTest(mappings=mappings):
                self.game._input_commands = mappings
                self.assertFalse(self.game.is_command_allowed(Command.ACCEPT))
                self.set_button(False)
                self.poll(0)
                self.set_button(True)
                self.assertIsNone(self.poll(1))
                self.assertIsNone(self.poll(4))

    async def test_hold_does_not_block_other_buttons(self):
        self.controller.product_id = JOYCON_PRO_PRODUCT_ID
        await self.game.on_message(json.dumps(input_collection({
            "VALID": self.command, "RIGHT": {"__class": "JD_Input_PhoneCommandData", "input": 7},
        })))
        self.set_button(True)
        self.assertIsNone(self.poll(0))
        self.set_button(True, "right")
        self.assertEqual(self.poll(0.1), Command.RIGHT)
        self.assertEqual(self.poll(2.01), Command.ACCEPT)

    async def test_hold_state_is_independent_for_each_game_connection(self):
        other = JustDanceGameV2(controller_with_buttons())
        self.assertFalse(other._takeover_hold_enabled)
        self.set_button(True)
        self.assertIsNone(self.poll(0))
        self.assertIsNone(other._confirm_hold_started_at)

    async def test_hold_uses_current_button_state_for_each_controller_type(self):
        for product, button in (
            (JOYCON_L_PRODUCT_ID, "right"), (JOYCON_R_PRODUCT_ID, "a"), (JOYCON_PRO_PRODUCT_ID, "a"),
        ):
            with self.subTest(product=product):
                self.controller.product_id = product
                for name in ("right", "a"):
                    self.set_button(False, name)
                self.poll(0)
                self.set_button(True, button)
                self.assertTrue(self.controller.is_command_pressed(Command.ACCEPT))
                self.assertIsNone(self.poll(1))
                self.assertEqual(self.poll(3.01), Command.ACCEPT)
                self.set_button(False, button)
                self.assertFalse(self.controller.is_command_pressed(Command.ACCEPT))

    async def test_command_loop_sends_supplied_valid_once_after_two_seconds(self):
        frames = iter(((0, True), (1.99, True), (2.01, True), (5, True), (5.1, False)))
        read_buttons = self.controller.get_latest_command
        now = 0

        def read_frame(commands_to_check):
            nonlocal now
            try:
                now, pressed = next(frames)
            except StopIteration:
                raise asyncio.CancelledError from None
            self.set_button(pressed)
            return read_buttons(commands_to_check)

        self.controller.get_latest_command = Mock(side_effect=read_frame)
        sent_times = []
        self.game.ws = SimpleNamespace(
            state=State.OPEN, send=AsyncMock(side_effect=lambda *args: sent_times.append(now)),
        )
        self.game.is_connected = True
        with patch("joydance.games.justdance_v2.time", SimpleNamespace(monotonic=lambda: now)):
            with self.assertRaises(asyncio.CancelledError):
                await self.game.send_command()
        self.assertEqual(sent_times, [2.01])
        self.game.ws.send.assert_awaited_once()
        self.assertEqual(json.loads(self.game.ws.send.await_args.args[0]), {"root": self.command})
