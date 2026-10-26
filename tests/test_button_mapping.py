import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from websockets.protocol import State

from joydance.constants import Command
from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.pycon.constants import JOYCON_R_PRODUCT_ID
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2


def controller_with_buttons():
    controller = JoyConWrapper.__new__(JoyConWrapper)
    controller.serial = "test-controller"
    controller.product_id = JOYCON_R_PRODUCT_ID
    controller.available_shortcuts = set()
    controller.joycon = SimpleNamespace(events=Mock(return_value=iter(())))
    return controller


def press(controller, button, commands_to_check=None):
    controller.joycon.events.return_value = iter([(button, 1)])
    return controller.get_latest_command(commands_to_check)


def menu_setup(shortcuts=()):
    return {
        "__class": "JD_PhoneUiSetupData",
        "inputSetup": {
            "isEnabled": 1,
            "carouselPosSetup": {"rowIndex": 0, "itemIndex": 0, "actionIndex": 0},
        },
        "setupData": {
            "mainCarousel": {
                "rows": [
                    {
                        "items": [
                            {"actions": [{"command": ""} for _ in range(3)]},
                            {"actions": [{"command": ""}]},
                        ]
                    },
                    {"items": [{"actions": [{"command": ""}]}]},
                ]
            },
            "shortcuts": [
                {
                    "command": json.dumps(
                        {
                            "root": {
                                "__class": "JD_Custom_PhoneCommandData",
                                "identifier": cmd.value,
                            }
                        }
                    )
                }
                for cmd in shortcuts
            ],
        },
    }


class ButtonMappingTests(unittest.TestCase):
    def test_basic_commands_work_without_screen_shortcuts(self):
        controller = controller_with_buttons()
        for button, expected in (
            ("a", Command.ACCEPT),
            ("right", Command.ACCEPT),
            ("b", Command.BACK),
            ("down", Command.BACK),
            ("plus", Command.PAUSE),
            ("minus", Command.PAUSE),
        ):
            with self.subTest(button=button):
                self.assertEqual(press(controller, button), expected)

    def test_profile_and_extras_use_separate_buttons_on_both_joycons(self):
        controller = controller_with_buttons()
        controller.available_shortcuts = {
            Command.ACTIVATE_DANCER_PROFILE,
            Command.SHORTCUT_EXTRA,
        }
        for button, expected in (
            ("x", Command.ACTIVATE_DANCER_PROFILE),
            ("y", Command.SHORTCUT_EXTRA),
            ("up", Command.ACTIVATE_DANCER_PROFILE),
            ("left", Command.SHORTCUT_EXTRA),
        ):
            with self.subTest(button=button):
                self.assertEqual(press(controller, button), expected)

    def test_plus_and_minus_use_menu_shortcuts_and_still_pause_during_gameplay(self):
        controller = controller_with_buttons()
        for button in ("plus", "minus"):
            for shortcut in (
                Command.DONT_SHOW_ANYMORE,
                Command.FAVORITE,
                Command.PLAYLIST_RENAME,
                Command.SAVE_PLAYLIST,
            ):
                with self.subTest(button=button, shortcut=shortcut):
                    controller.available_shortcuts = {shortcut, Command.PAUSE}
                    self.assertEqual(press(controller, button), shortcut)
                    self.assertEqual(
                        press(controller, button, {Command.PAUSE}), Command.PAUSE
                    )
            controller.available_shortcuts = set()
            self.assertEqual(press(controller, button, {Command.PAUSE}), Command.PAUSE)

    def test_trigger_shortcuts_take_priority_over_action_cycling(self):
        controller = controller_with_buttons()
        for button in ("zr", "zl"):
            with self.subTest(button=button):
                controller.available_shortcuts = {Command.NEXT_ACTION}
                self.assertEqual(press(controller, button), Command.NEXT_ACTION)
                controller.available_shortcuts.add(Command.TIPS_NEXT)
                self.assertEqual(press(controller, button), Command.TIPS_NEXT)

    def test_upload_uses_y_or_left_only_when_available(self):
        controller = controller_with_buttons()
        for button in ("y", "left"):
            with self.subTest(button=button):
                self.assertIsNone(press(controller, button))
                controller.available_shortcuts = {Command.UPLOAD}
                self.assertEqual(press(controller, button), Command.UPLOAD)
                controller.available_shortcuts = set()

    def test_releases_and_filtered_out_buttons_do_not_send_commands(self):
        controller = controller_with_buttons()
        controller.joycon.events.return_value = iter([("a", 0)])
        self.assertIsNone(controller.get_latest_command())
        self.assertIsNone(press(controller, "a", {Command.PAUSE}))
        self.assertIsNone(press(controller, "a", set()))
        self.assertIsNone(press(controller, "x", {Command.ACTIVATE_DANCER_PROFILE}))


class GameInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = controller_with_buttons()
        self.game = JustDanceGameV1(
            self.controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock()
        )

    async def test_navigation_ownership_is_published_without_changing_player_number_or_leds(self):
        updates = []
        self.game._on_state_changed = AsyncMock(side_effect=lambda serial, state: updates.append(state.copy()))
        self.game.profile_data = {"player_id": 2}
        self.controller.set_player_led = AsyncMock()
        await self.game.on_message(json.dumps(menu_setup()))
        self.assertEqual(updates, [{"is_main_player": True}])
        await self.game.on_message(json.dumps(menu_setup()))
        self.assertEqual(len(updates), 1)
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiSetupData",
            "inputSetup": {"isEnabled": 1},
            "setupData": {"__class": "JD_SimplePhoneUiData"},
        }))
        self.assertEqual(updates[-1], {"is_main_player": False})
        popup = menu_setup()
        popup["isPopup"] = 1
        await self.game.on_message(json.dumps(popup))
        self.assertEqual(len(updates), 2)
        self.assertFalse((await self.game.get_state(pull_new_data=False))["is_main_player"])
        self.assertEqual(self.game.profile_data["player_id"], 2)
        self.controller.set_player_led.assert_not_awaited()

    async def test_secondary_player_can_confirm_takeover_popup_then_navigate(self):
        self.controller.set_player_led = AsyncMock()
        for explicit_action in (False, True):
            with self.subTest(explicit_action=explicit_action):
                await self.game.on_message(json.dumps({
                    "__class": "JD_ProfilePhoneUiData", "playerId": 1,
                }))
                await self.game.on_message(json.dumps({
                    "__class": "JD_PhoneUiSetupData",
                    "inputSetup": {"isEnabled": 1},
                    "setupData": {"__class": "JD_SimplePhoneUiData"},
                }))
                self.assertFalse(self.game.is_main_player)
                self.assertFalse(self.game.is_input_allowed)

                popup = menu_setup()
                popup["isPopup"] = 1
                # Captured from the takeover popup; the app forwards this supplied command.
                action = {"__class": "JD_Input_PhoneCommandData", "input": 185785632}
                if explicit_action:
                    popup["setupData"]["mainCarousel"]["rows"][0]["items"][0]["actions"] = [
                        {"command": json.dumps({"root": action})}
                    ]
                await self.game.on_message(json.dumps(popup))
                self.assertFalse(self.game.is_main_player)
                self.assertTrue(self.game.is_input_allowed)

                self.controller.joycon.events.return_value = iter([("a", 1)])
                self.controller.get_joystick_command = Mock(return_value=None)
                self.game.is_connected = True
                self.game.ws = SimpleNamespace(state=State.OPEN)
                sent = asyncio.Event()
                self.game.send_message = AsyncMock(side_effect=lambda *args: sent.set())
                task = asyncio.create_task(self.game.send_command())
                try:
                    await asyncio.wait_for(sent.wait(), 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                expected = (
                    ("JD_Input_PhoneCommandData", {"input": action["input"]})
                    if explicit_action else (
                        "ValidateAction_PhoneCommandData",
                        {"rowIndex": 0, "itemIndex": 0, "actionIndex": 0},
                    )
                )
                self.game.send_message.assert_awaited_once_with(*expected)
                self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), expected)

                await self.game.on_message(json.dumps(menu_setup()))
                await self.game.on_message(json.dumps({
                    "__class": "JD_ProfilePhoneUiData", "playerId": 1,
                }))
                self.assertEqual(self.game.profile_data["player_id"], 2)
                self.assertTrue(self.game.is_main_player)
                self.assertEqual(await self.game.preprocess_command(Command.RIGHT), (
                    "ChangeItem_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1},
                ))

    async def test_closing_secondary_player_popup_restores_navigation_restrictions(self):
        self.controller.set_player_led = AsyncMock()
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiSetupData",
            "inputSetup": {"isEnabled": 1},
            "setupData": {"__class": "JD_SimplePhoneUiData"},
        }))
        await self.game.on_message(json.dumps({
            "__class": "JD_ProfilePhoneUiData", "playerId": 0,
        }))
        self.assertFalse(self.game.is_main_player)
        popup = menu_setup()
        popup["isPopup"] = 1
        await self.game.on_message(json.dumps(popup))
        await self.game.on_message(json.dumps({"__class": "JD_ClosePopup_ConsoleCommandData"}))
        self.assertFalse(self.game.is_main_player)
        for command in (Command.ACCEPT, Command.LEFT, Command.RIGHT):
            self.assertEqual(await self.game.preprocess_command(command), (None, None))

    async def test_trigger_cycles_actions_and_accept_confirms_selected_action(self):
        await self.game.on_message(json.dumps(menu_setup()))
        for expected_index in (1, 2, 0):
            cmd = press(self.controller, "zr")
            self.assertEqual(cmd, Command.NEXT_ACTION)
            self.assertEqual(
                await self.game.preprocess_command(cmd),
                (
                    "ChangeAction_PhoneCommandData",
                    {"rowIndex": 0, "itemIndex": 0, "actionIndex": expected_index},
                ),
            )
            self.assertEqual(
                await self.game.preprocess_command(press(self.controller, "a")),
                (
                    "ValidateAction_PhoneCommandData",
                    {"rowIndex": 0, "itemIndex": 0, "actionIndex": expected_index},
                ),
            )

    async def test_navigation_resets_action_and_single_action_items_do_not_cycle(self):
        for direction in (Command.UP, Command.DOWN, Command.LEFT, Command.RIGHT):
            with self.subTest(direction=direction):
                await self.game.on_message(json.dumps(menu_setup()))
                await self.game.preprocess_command(Command.NEXT_ACTION)
                await self.game.preprocess_command(direction)
                self.assertEqual(self.game.action_id, 0)
                self.assertEqual(
                    await self.game.preprocess_command(Command.NEXT_ACTION),
                    (None, None),
                )

    async def test_action_cycling_respects_input_and_main_player_restrictions(self):
        await self.game.on_message(json.dumps(menu_setup()))
        for attr in ("is_input_allowed", "is_main_player", "is_in_lobby"):
            with self.subTest(attr=attr):
                previous = getattr(self.game, attr)
                setattr(self.game, attr, attr == "is_in_lobby")
                self.assertEqual(
                    await self.game.preprocess_command(Command.NEXT_ACTION),
                    (None, None),
                )
                self.assertEqual(self.game.action_id, 0)
                setattr(self.game, attr, previous)

    async def test_new_screens_clear_shortcuts_in_both_protocols(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = game_class(
                    self.controller,
                    on_state_changed=AsyncMock(),
                    on_game_message=AsyncMock(),
                )
                self.controller.available_shortcuts = {
                    Command.SAVE_PLAYLIST,
                    Command.NEXT_ACTION,
                }
                await game.on_message(
                    json.dumps(
                        {
                            "__class": "JD_PhoneUiSetupData",
                            "setupData": {
                                "gameplaySetup": {"pauseSlider": {"isEnabled": 1}}
                            },
                        }
                    )
                )
                self.assertEqual(self.controller.available_shortcuts, {Command.PAUSE})
                await game.on_message(
                    json.dumps(
                        {
                            "__class": "JD_PhoneUiSetupData",
                            "setupData": {},
                        }
                    )
                )
                self.assertEqual(self.controller.available_shortcuts, set())

    async def test_v1_parser_rebuilds_shortcuts_without_shared_handler(self):
        await self.game.parse_phone_setup_data(menu_setup((Command.UPLOAD,)))
        self.assertEqual(
            self.game.available_shortcuts, {Command.UPLOAD, Command.NEXT_ACTION}
        )
        await self.game.parse_phone_setup_data(
            {"setupData": {"gameplaySetup": {"pauseSlider": {"isEnabled": 1}}}}
        )
        self.assertEqual(self.game.available_shortcuts, {Command.PAUSE})
        self.assertEqual(self.controller.available_shortcuts, {Command.PAUSE})
        for data in ({}, {"setupData": {"shortcuts": None}}):
            with self.subTest(data=data):
                self.game.available_shortcuts = {Command.PAUSE}
                self.controller.available_shortcuts = {Command.PAUSE}
                await self.game.parse_phone_setup_data(data)
                self.assertEqual(self.game.available_shortcuts, set())
                self.assertEqual(self.controller.available_shortcuts, set())

    async def test_upload_is_parsed_and_sent_in_both_protocols(self):
        await self.game.on_message(json.dumps(menu_setup((Command.UPLOAD,))))
        self.assertEqual(
            await self.game.preprocess_command(press(self.controller, "y")),
            (
                "JD_Custom_PhoneCommandData",
                {"identifier": "SHORTCUT_UPLOAD"},
            ),
        )
        v2 = JustDanceGameV2(self.controller)
        self.assertEqual(
            await v2.preprocess_command(Command.UPLOAD),
            (
                "JD_Custom_PhoneCommandData",
                {"identifier": "SHORTCUT_UPLOAD"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
