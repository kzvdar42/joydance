import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.protocol import State

from joydance.constants import Command
from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.pycon.constants import JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID
from joydance.controllers.joycon.pycon.event import ButtonEventJoyCon
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
                            {"isEnabled": True, "actions": [{"command": ""} for _ in range(3)]},
                            {"isEnabled": True, "actions": [{"command": ""}]},
                        ]
                    },
                    {"items": [{"isEnabled": True, "actions": [{"command": ""}]}]},
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


def lobby_setup(selected_coach, coach_ids=(0, 1, 2)):
    return {
        "__class": "JD_PhoneUiSetupData",
        "inputSetup": {"isEnabled": 1},
        "setupData": {"__class": "JD_PhoneUiData", "lobbySetup": {
            "coaches": [
                {"selectedOnEnter": index == selected_coach, "actions": [{"command": json.dumps({
                    "root": {"__class": "JD_ChangeCoach_PhoneCommandData", "coachId": coach_id},
                })}]}
                for index, coach_id in enumerate(coach_ids)
            ],
        }},
    }


def input_collection(commands):
    return {
        "__class": "JD_InputCollectionPhoneData",
        "availableInput": {key: json.dumps({"root": command}) for key, command in commands.items()},
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

    async def test_coach_only_lobby_does_not_mark_secondary_controller_as_main(self):
        await self.game.on_message(json.dumps(menu_setup()))
        # Captured secondary lobby: full JD_PhoneUiData with coaches, without Start.
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiSetupData", "inputSetup": {"isEnabled": 1},
            "setupData": {"__class": "JD_PhoneUiData", "lobbySetup": {
                "coaches": [{"selectedOnEnter": 0}, {"selectedOnEnter": 1}],
            }},
        }))
        self.assertFalse(self.game.is_main_player)
        self.assertFalse(self.game._state["is_main_player"])
        self.assertTrue(self.game.is_input_allowed)
        self.assertTrue(self.game.is_in_lobby)
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (None, None))

    async def test_disabled_lobby_start_still_identifies_main_controller(self):
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiSetupData", "inputSetup": {"isEnabled": 0},
            "setupData": {"__class": "JD_PhoneUiData", "lobbySetup": {
                "coaches": [{}, {}], "startButton": {"isEnabled": False},
            }},
        }))
        self.assertTrue(self.game.is_main_player)
        self.assertTrue(self.game._state["is_main_player"])
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (None, None))
        for enabled in (True, False):
            await self.game.on_message(json.dumps({
                "__class": "InputSetup_ConsoleCommandData", "isEnabled": enabled,
            }))
            await self.game.on_message(json.dumps({
                "__class": "JD_EnableLobbyStartbutton_ConsoleCommandData", "isEnabled": enabled,
            }))
            self.assertTrue(self.game.is_main_player)

    async def test_shared_screens_preserve_navigation_ownership(self):
        for is_main in (False, True):
            with self.subTest(is_main=is_main):
                if is_main:
                    await self.game.on_message(json.dumps(menu_setup()))
                for setup in (
                    {"gameplaySetup": {"pauseSlider": {"isEnabled": 1}}},
                    {"recapSetup": {"title": "Results"}, "mainCarousel": {"rows": []}},
                    {"recapSetup": {}, "mainCarousel": {"rows": []}},
                    {},
                ):
                    await self.game.on_message(json.dumps({
                        "__class": "JD_PhoneUiSetupData", "inputSetup": {"isEnabled": 1},
                        "setupData": {"__class": "JD_PhoneUiData", **setup},
                    }))
                    self.assertEqual(self.game.is_main_player, is_main)
                    self.assertEqual(self.game._state["is_main_player"], is_main)

    async def test_popup_lobby_does_not_change_navigation_ownership(self):
        for is_main in (False, True):
            with self.subTest(is_main=is_main):
                if is_main:
                    await self.game.on_message(json.dumps(menu_setup()))
                await self.game.on_message(json.dumps({
                    "__class": "JD_PhoneUiSetupData", "isPopup": 1,
                    "inputSetup": {"isEnabled": 1},
                    "setupData": {"lobbySetup": {"coaches": [{}, {}]}},
                }))
                await self.game.on_message(json.dumps({"__class": "JD_ClosePopup_ConsoleCommandData"}))
                self.assertEqual(self.game.is_main_player, is_main)
                self.assertEqual(self.game._state["is_main_player"], is_main)

    async def test_disconnect_clears_navigation_ownership_before_reconnecting(self):
        self.controller.set_player_led = AsyncMock()
        self.controller.set_accel_collection = Mock()
        await self.game.on_message(json.dumps(menu_setup()))
        self.game.is_connected = True
        await self.game.disconnect(should_reconnect=False)
        self.assertFalse(self.game.is_main_player)
        self.assertFalse(self.game._state["is_main_player"])
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiSetupData", "inputSetup": {"isEnabled": 1},
            "setupData": {"gameplaySetup": {"pauseSlider": {"isEnabled": 1}}},
        }))
        self.assertFalse(self.game.is_main_player)
        self.assertFalse(self.game._state["is_main_player"])

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

    async def test_action_cycling_respects_input_and_lobby_restrictions(self):
        await self.game.on_message(json.dumps(menu_setup()))
        for attr in ("is_input_allowed", "is_in_lobby"):
            with self.subTest(attr=attr):
                previous = getattr(self.game, attr)
                setattr(self.game, attr, attr == "is_in_lobby")
                self.assertEqual(
                    await self.game.preprocess_command(Command.NEXT_ACTION),
                    (None, None),
                )
                self.assertEqual(self.game.action_id, 0)
                setattr(self.game, attr, previous)

    async def test_non_main_player_can_use_controls_enabled_by_the_game(self):
        await self.game.on_message(json.dumps(menu_setup((Command.UPLOAD,))))
        self.game.is_main_player = False
        for command, expected in (
            (Command.NEXT_ACTION, (
                "ChangeAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 0, "actionIndex": 1},
            )),
            (Command.ACCEPT, (
                "ValidateAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 0, "actionIndex": 1},
            )),
            (Command.RIGHT, ("ChangeItem_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1})),
            (Command.UPLOAD, ("JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value})),
        ):
            with self.subTest(command=command):
                self.assertEqual(await self.game.preprocess_command(command), expected)
        self.assertFalse(self.game.is_main_player)
        await self.game.on_message(json.dumps({
            "__class": "InputSetup_ConsoleCommandData", "isEnabled": False,
        }))
        for command in (Command.NEXT_ACTION, Command.ACCEPT, Command.RIGHT, Command.UPLOAD):
            self.assertEqual(await self.game.preprocess_command(command), (None, None))

    async def test_non_main_player_lobby_uses_coach_and_start_permissions(self):
        setup = lobby_setup(selected_coach=0, coach_ids=(0, 1))
        setup["setupData"]["lobbySetup"]["startButton"] = {
            "isEnabled": False, "actions": [{"command": json.dumps({
                "root": {"__class": "JD_StartGame_PhoneCommandData"},
            })}],
        }
        await self.game.on_message(json.dumps(setup))
        self.game.is_main_player = False
        for direction, coach_id in ((Command.RIGHT, 1), (Command.LEFT, 0)):
            self.assertEqual(await self.game.preprocess_command(direction), (
                "JD_ChangeCoach_PhoneCommandData", {"coachId": coach_id},
            ))
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (None, None))
        await self.game.on_message(json.dumps({
            "__class": "JD_EnableLobbyStartbutton_ConsoleCommandData", "isEnabled": True,
        }))
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (
            "JD_StartGame_PhoneCommandData", {},
        ))

    async def test_lobby_uses_game_selected_coach_and_respects_boundaries(self):
        self.game.send_message = AsyncMock()
        for with_start in (False, True):
            with self.subTest(with_start=with_start):
                setup = lobby_setup(selected_coach=1)
                setup["inputSetup"]["carouselPosSetup"] = {
                    "rowIndex": 0, "itemIndex": 0, "actionIndex": 0,
                }
                if with_start:
                    setup["setupData"]["lobbySetup"]["startButton"] = {"isEnabled": False}
                await self.game.on_message(json.dumps(setup))
                self.assertEqual(self.game.coach_id, 1)
                self.game.send_message.assert_not_awaited()
                for direction, coach_id in (
                    (Command.LEFT, 0), (Command.LEFT, None), (Command.RIGHT, 1),
                    (Command.RIGHT, 2), (Command.RIGHT, None),
                ):
                    expected = (None, None) if coach_id is None else (
                        "JD_ChangeCoach_PhoneCommandData", {"coachId": coach_id},
                    )
                    self.assertEqual(await self.game.preprocess_command(direction), expected)

    async def test_coach_selection_uses_supplied_actions_without_mutating_them(self):
        await self.game.on_message(json.dumps(lobby_setup(selected_coach=1, coach_ids=(7, 3, 9))))
        for direction, coach_id in (
            (Command.RIGHT, 9), (Command.LEFT, 3), (Command.LEFT, 7), (Command.RIGHT, 3),
        ):
            self.assertEqual(await self.game.preprocess_command(direction), (
                "JD_ChangeCoach_PhoneCommandData", {"coachId": coach_id},
            ))

    async def test_coach_without_an_action_sends_no_command(self):
        for coach in ({}, {"actions": []}, {"actions": [{}]}, {"actions": [{"command": ""}]}):
            with self.subTest(coach=coach):
                setup = lobby_setup(selected_coach=0)
                setup["setupData"]["lobbySetup"]["coaches"][1] = coach
                await self.game.on_message(json.dumps(setup))
                for direction, coach_id in (
                    (Command.RIGHT, None), (Command.RIGHT, 2),
                    (Command.LEFT, None), (Command.LEFT, 0),
                ):
                    expected = (None, None) if coach_id is None else (
                        "JD_ChangeCoach_PhoneCommandData", {"coachId": coach_id},
                    )
                    self.assertEqual(await self.game.preprocess_command(direction), expected)
                    self.assertEqual(self.game.coach_id, 1 if coach_id is None else coach_id)

    async def test_new_lobby_replaces_coach_selection_without_carousel_position(self):
        for selected in (2, 1, None):
            with self.subTest(selected=selected):
                await self.game.on_message(json.dumps(lobby_setup(selected_coach=selected)))
                self.assertEqual(self.game.coach_id, selected if selected is not None else 0)
        await self.game.on_message(json.dumps(lobby_setup(selected_coach=None, coach_ids=())))
        self.assertEqual(self.game.coach_id, 0)
        for direction in (Command.LEFT, Command.RIGHT):
            self.assertEqual(await self.game.preprocess_command(direction), (None, None))
            self.assertEqual(self.game.coach_id, 0)

    async def test_popup_preserves_game_selected_coach_and_actions(self):
        await self.game.on_message(json.dumps(lobby_setup(selected_coach=1, coach_ids=(7, 3, 9))))
        for popup in (menu_setup(), lobby_setup(selected_coach=0, coach_ids=(10,))):
            popup["isPopup"] = True
            await self.game.on_message(json.dumps(popup))
        await self.game.on_message(json.dumps({"__class": "JD_ClosePopup_ConsoleCommandData"}))
        self.assertEqual(self.game.coach_id, 1)
        self.assertEqual(await self.game.preprocess_command(Command.RIGHT), (
            "JD_ChangeCoach_PhoneCommandData", {"coachId": 9},
        ))

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

    async def test_v1_parser_rebuilds_shortcuts_when_called_directly(self):
        await self.game.parse_phone_setup_data(menu_setup((Command.UPLOAD,)))
        self.assertEqual(
            self.game.available_shortcuts, {Command.UPLOAD, Command.NEXT_ACTION}
        )
        await self.game.parse_phone_setup_data(
            {"setupData": {"gameplaySetup": {"pauseSlider": {"isEnabled": 1}}}}
        )
        self.assertEqual(self.game.available_shortcuts, {Command.PAUSE})
        self.assertEqual(self.controller.available_shortcuts, {Command.PAUSE})
        for data in ({"setupData": {}}, {"setupData": {"shortcuts": None}}):
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
        await v2.parse_phone_setup_data(menu_setup())
        v2.available_shortcuts = {Command.UPLOAD}
        self.assertEqual(
            await v2.preprocess_command(Command.UPLOAD),
            (
                "JD_Custom_PhoneCommandData",
                {"identifier": "SHORTCUT_UPLOAD"},
            ),
        )

    async def test_v1_shortcuts_send_complete_commands_repeatedly(self):
        commands = (
            ("y", {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.UPLOAD.value,
                   "targetId": 42, "options": {"enabled": False, "ids": [1, 2]}}),
            ("x", {"__class": "JD_Input_PhoneCommandData", "input": Command.V1_FAVORITE.value,
                   "targetId": 0}),
            ("y", {"__class": "JD_TargetedShortcut_PhoneCommandData", "identifier": Command.UPLOAD.value,
                   "targetId": 7}),
        )
        self.game.ws = SimpleNamespace(state=State.OPEN, send=AsyncMock())
        for button, command in commands:
            with self.subTest(command=command):
                setup = menu_setup()
                setup["setupData"]["shortcuts"] = [{"command": json.dumps({"root": command})}]
                await self.game.on_message(json.dumps(setup))
                for _ in range(2):
                    command_class, data = await self.game.preprocess_command(press(self.controller, button))
                    await self.game.send_message(command_class, data)
                    self.assertEqual(json.loads(self.game.ws.send.await_args.args[0]), {"root": command})

    async def test_v1_shortcut_payloads_follow_popup_scope_and_screen_replacement(self):
        main_command = {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.UPLOAD.value, "targetId": 42}
        popup_command = {**main_command, "targetId": 7}
        setup = menu_setup()
        setup["setupData"]["shortcuts"] = [{"command": json.dumps({"root": main_command})}]
        await self.game.on_message(json.dumps(setup))
        popup = menu_setup()
        popup["isPopup"] = True
        popup["setupData"]["shortcuts"] = [{"command": json.dumps({"root": popup_command})}]
        await self.game.on_message(json.dumps(popup))
        await self.game.on_message(json.dumps({"__class": "JD_PhoneUiSetupData"}))
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (
            "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value, "targetId": 7},
        ))
        await self.game.on_message(json.dumps({"__class": "JD_ClosePopup_ConsoleCommandData"}))
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (
            "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value, "targetId": 42},
        ))
        await self.game.on_message(json.dumps(menu_setup()))
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (None, None))
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiShortcutData", "shortcuts": [{
                "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
            }],
        }))
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (
            "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value},
        ))

    async def test_v1_disconnect_discards_supplied_shortcut_payloads(self):
        command = {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.UPLOAD.value, "targetId": 42}
        setup = menu_setup()
        setup["setupData"]["shortcuts"] = [{"command": json.dumps({"root": command})}]
        await self.game.on_message(json.dumps(setup))
        self.controller.set_accel_collection = Mock()
        self.controller.set_player_led = AsyncMock()
        await self.game.disconnect(should_reconnect=False)
        await self.game.on_message(json.dumps({"__class": "InputSetup_ConsoleCommandData", "isEnabled": True}))
        await self.game.on_message(json.dumps({
            "__class": "JD_PhoneUiShortcutData", "shortcuts": [{
                "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
            }],
        }))
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (
            "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value},
        ))

    async def test_v1_keyboard_cancel_takes_priority_over_back_shortcut(self):
        command = {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.BACK.value, "targetId": 42}
        setup = menu_setup()
        setup["setupData"]["shortcuts"] = [{"command": json.dumps({"root": command})}]
        await self.game.on_message(json.dumps(setup))
        self.assertEqual(await self.game.preprocess_command(Command.BACK), (
            "JD_Custom_PhoneCommandData", {"identifier": Command.BACK.value, "targetId": 42},
        ))
        await self.game.on_message(json.dumps({"__class": "JD_OpenPhoneKeyboard_ConsoleCommandData"}))
        self.assertEqual(await self.game.preprocess_command(Command.BACK), ("JD_CancelKeyboard_PhoneCommandData", {}))

    async def test_v1_invalid_shortcut_payloads_do_not_replace_valid_commands(self):
        command = {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.UPLOAD.value, "targetId": 42}
        for invalid in (
            "invalid JSON", json.dumps({"root": None}), json.dumps({"root": []}),
            json.dumps({"root": {"identifier": Command.UPLOAD.value}}),
            json.dumps({"root": {"__class": "", "identifier": Command.UPLOAD.value}}),
            json.dumps({"root": {"__class": 7, "identifier": Command.UPLOAD.value}}),
        ):
            with self.subTest(invalid=invalid):
                setup = menu_setup()
                setup["setupData"]["shortcuts"] = [{"command": json.dumps({"root": command})}, {"command": invalid}]
                with self.assertLogs("joydance", level="ERROR"):
                    await self.game.on_message(json.dumps(setup))
                self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (
                    "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value, "targetId": 42},
                ))


class BufferedButtons:
    events = ButtonEventJoyCon.events
    joycon_button_event = ButtonEventJoyCon.joycon_button_event

    def __init__(self):
        self._events_buffer = []


class InputPermissionTests(unittest.IsolatedAsyncioTestCase):
    def make_game(self, game_class):
        controller = controller_with_buttons()
        controller.joycon = BufferedButtons()
        controller.get_joystick_command = Mock(return_value=None)
        return game_class(
            controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock()
        )

    async def message(self, game, name, **data):
        await game.on_message(json.dumps({"__class": name, **data}))

    async def test_partial_ui_updates_preserve_lobby_coaches_and_shortcuts(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                setup = lobby_setup(selected_coach=1)
                setup["setupData"]["lobbySetup"]["startButton"] = {"isEnabled": True}
                setup["setupData"]["gameplaySetup"] = {"pauseSlider": {"isEnabled": True}}
                await game.on_message(json.dumps(setup))
                if game_class is JustDanceGameV1:
                    await game.preprocess_command(Command.RIGHT)
                    self.assertEqual(game.coach_id, 2)
                for update, enabled in (
                    ({}, True),
                    ({"setupData": None, "inputSetup": None}, True),
                    ({"inputSetup": {"isEnabled": False}}, False),
                    ({"inputSetup": {"isEnabled": True}}, True),
                ):
                    await self.message(game, "JD_PhoneUiSetupData", **update)
                    self.assertTrue(game.is_in_lobby)
                    self.assertTrue(game.is_lobby_start_allowed)
                    self.assertEqual(game.is_input_allowed, enabled)
                    self.assertEqual(game.available_shortcuts, {Command.PAUSE})
                    self.assertEqual(game.controller.available_shortcuts, {Command.PAUSE})
                    if game_class is JustDanceGameV1:
                        self.assertTrue(game.is_main_player)
                        self.assertEqual(game.num_coaches, 3)
                        self.assertEqual(game.coach_id, 2)
                if game_class is JustDanceGameV1:
                    self.assertEqual(await game.preprocess_command(Command.LEFT), (
                        "JD_ChangeCoach_PhoneCommandData", {"coachId": 1},
                    ))

    async def test_partial_ui_input_targets_its_layer_without_changing_popup_focus(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                setup = menu_setup()
                setup["setupData"]["mainCarousel"]["rows"][1]["items"][0]["isEnabled"] = False
                await game.on_message(json.dumps(setup))
                await self.message(game, "JD_PhoneUiShortcutData", applyOnPopup=False, shortcuts=[{
                    "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
                }])
                popup = menu_setup()
                popup["isPopup"] = True
                popup["inputSetup"]["carouselPosSetup"]["itemIndex"] = 1
                popup["setupData"]["mainCarousel"]["rows"][0]["items"][0]["isEnabled"] = False
                await game.on_message(json.dumps(popup))
                await self.message(game, "JD_PhoneUiShortcutData", applyOnPopup=True, shortcuts=[{
                    "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.SKIP.value,
                }])

                await self.message(game, "JD_PhoneUiSetupData", inputSetup={
                    "isEnabled": False, "applyOnPopup": False,
                    "carouselPosSetup": {"rowIndex": 1, "itemIndex": 0, "actionIndex": 0},
                })
                self.assertTrue(game.is_input_allowed)
                self.assertEqual(game.available_shortcuts, {Command.SKIP})
                if game_class is JustDanceGameV1:
                    self.assertEqual((game.row_num, game.col_num_per_row_id[0]), (0, 1))

                for enabled in (False, True):
                    await self.message(game, "JD_PhoneUiSetupData", inputSetup={
                        "isEnabled": enabled, "applyOnPopup": True,
                        "carouselPosSetup": {"rowIndex": 0, "itemIndex": 0, "actionIndex": 0},
                    })
                    self.assertEqual(game.is_input_allowed, enabled)
                    self.assertEqual(game.available_shortcuts, {Command.SKIP})
                    self.assertEqual(game.controller.available_shortcuts, {Command.SKIP})
                if game_class is JustDanceGameV1:
                    self.assertFalse(game.is_command_allowed(Command.ACCEPT))
                await self.message(game, "JD_ClosePopup_ConsoleCommandData")
                self.assertFalse(game.is_input_allowed)
                self.assertEqual(game.available_shortcuts, {Command.UPLOAD})
                await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True)
                if game_class is JustDanceGameV1:
                    self.assertEqual(game.row_num, 1)
                    self.assertFalse(game.is_command_allowed(Command.ACCEPT))

    async def test_v2_uses_server_provided_direction_and_confirm_commands(self):
        game = self.make_game(JustDanceGameV2)
        await game.on_message(json.dumps(menu_setup()))
        mappings = (
            ("UP", Command.UP, {"__class": "JD_Input_PhoneCommandData", "input": 101}),
            ("RIGHT", Command.RIGHT, {"__class": "JD_Input_PhoneCommandData", "input": 102}),
            ("DOWN", Command.DOWN, {"__class": "JD_Input_PhoneCommandData", "input": 103}),
            ("LEFT", Command.LEFT, {"__class": "JD_Input_PhoneCommandData", "input": 104}),
            ("VALID", Command.ACCEPT, {
                "__class": "JD_Custom_PhoneCommandData", "identifier": "CONFIRM", "extra": {"value": 7},
            }),
        )
        await game.on_message(json.dumps(input_collection({name: data for name, _, data in mappings})))
        for name, cmd, data in mappings:
            with self.subTest(input=name):
                expected = (data["__class"], {key: value for key, value in data.items() if key != "__class"})
                self.assertTrue(game.is_command_allowed(cmd))
                self.assertEqual(await game.preprocess_command(cmd), expected)
                self.assertEqual(await game.preprocess_command(cmd), expected)

    async def test_v2_mapping_replacement_does_not_reuse_missing_commands(self):
        game = self.make_game(JustDanceGameV2)
        await game.on_message(json.dumps(menu_setup()))
        valid = {"__class": "JD_Input_PhoneCommandData", "input": 185785632}
        await game.on_message(json.dumps(input_collection({"VALID": valid})))
        await game.on_message(json.dumps(input_collection({"RIGHT": valid})))
        self.assertFalse(game.is_command_allowed(Command.ACCEPT))
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
        self.assertEqual(await game.preprocess_command(Command.RIGHT), (
            "JD_Input_PhoneCommandData", {"input": 185785632},
        ))
        await game.on_message(json.dumps(input_collection({})))
        for cmd in (Command.UP, Command.RIGHT, Command.DOWN, Command.LEFT, Command.ACCEPT):
            self.assertFalse(game.is_command_allowed(cmd))
            self.assertEqual(await game.preprocess_command(cmd), (None, None))

    async def test_v2_invalid_mappings_are_ignored_without_falling_back(self):
        for encoded in (
            None, "", 1, {}, "not json", "null", "[]", "{}",
            '{"root": []}', '{"root": {"input": 1}}', '{"root": {"__class": 1}}',
            '{"root": {"__class": ""}}',
        ):
            with self.subTest(encoded=encoded):
                game = self.make_game(JustDanceGameV2)
                await game.on_message(json.dumps(menu_setup()))
                initial = input_collection({"VALID": {"__class": "JD_Input_PhoneCommandData", "input": 7}})
                await game.on_message(json.dumps(initial))
                replacement = input_collection({"RIGHT": {"__class": "JD_Input_PhoneCommandData", "input": 9}})
                replacement["availableInput"]["VALID"] = encoded
                with patch("joydance.games.justdance_v2.logger"):
                    await game.on_message(json.dumps(replacement))
                self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
                self.assertEqual(await game.preprocess_command(Command.RIGHT), (
                    "JD_Input_PhoneCommandData", {"input": 9},
                ))
        for invalid_table in (None, [], "invalid"):
            with self.subTest(available_input=invalid_table):
                with patch("joydance.games.justdance_v2.logger"):
                    await self.message(game, "JD_InputCollectionPhoneData", availableInput=invalid_table)
                self.assertEqual(await game.preprocess_command(Command.RIGHT), (None, None))

    async def test_v2_mapping_updates_do_not_enable_input_or_change_shortcuts_and_pause(self):
        game = self.make_game(JustDanceGameV2)
        await game.on_message(json.dumps(menu_setup()))
        await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=False)
        await game.on_message(json.dumps(input_collection({
            "VALID": {"__class": "JD_Input_PhoneCommandData", "input": 185785632},
        })))
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
        await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True)
        await self.message(game, "JD_PhoneUiShortcutData", shortcuts=[{
            "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
        }])
        self.assertEqual(await game.preprocess_command(Command.UPLOAD), (
            "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value},
        ))
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (
            "JD_Input_PhoneCommandData", {"input": 185785632},
        ))
        game.should_start_accelerometer = True
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
        self.assertEqual(await game.preprocess_command(Command.PAUSE), ("JD_Pause_PhoneCommandData", {}))

    async def test_v2_input_mappings_are_independent_for_each_connection_and_leave_v1_unchanged(self):
        first, second = self.make_game(JustDanceGameV2), self.make_game(JustDanceGameV2)
        legacy = self.make_game(JustDanceGameV1)
        for game, value in ((first, 7), (second, 9), (legacy, 11)):
            await game.on_message(json.dumps(menu_setup()))
            await game.on_message(json.dumps(input_collection({
                "VALID": {"__class": "JD_Input_PhoneCommandData", "input": value},
            })))
        self.assertEqual(await first.preprocess_command(Command.ACCEPT), ("JD_Input_PhoneCommandData", {"input": 7}))
        self.assertEqual(await second.preprocess_command(Command.ACCEPT), ("JD_Input_PhoneCommandData", {"input": 9}))
        self.assertEqual(await legacy.preprocess_command(Command.ACCEPT), (
            "ValidateAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 0, "actionIndex": 0},
        ))

    async def test_v2_mappings_survive_screen_and_popup_changes_but_reset_on_disconnect(self):
        game = self.make_game(JustDanceGameV2)
        await game.on_message(json.dumps(input_collection({
            "VALID": {"__class": "JD_Input_PhoneCommandData", "input": 185785632},
        })))
        for is_popup in (False, True):
            setup = menu_setup()
            setup["isPopup"] = is_popup
            await game.on_message(json.dumps(setup))
            self.assertEqual(await game.preprocess_command(Command.ACCEPT), (
                "JD_Input_PhoneCommandData", {"input": 185785632},
            ))
        await self.message(game, "JD_ClosePopup_ConsoleCommandData")
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (
            "JD_Input_PhoneCommandData", {"input": 185785632},
        ))
        game.controller.set_accel_collection = Mock()
        game.controller.set_player_led = AsyncMock()
        await game.disconnect(should_reconnect=False)
        await game.on_message(json.dumps(menu_setup()))
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (
            "JD_Input_PhoneCommandData", {"input": Command.ACCEPT.value},
        ))

    async def test_v2_mapping_update_discards_presses_for_the_previous_mapping(self):
        game = self.make_game(JustDanceGameV2)
        await game.on_message(json.dumps(menu_setup()))
        game.controller.joycon.joycon_button_event("a", 1)
        await game.on_message(json.dumps(input_collection({
            "VALID": {"__class": "JD_Input_PhoneCommandData", "input": 185785632},
        })))
        self.assertEqual(game.controller.joycon._events_buffer, [])

    async def test_v2_button_press_sends_the_supplied_command_on_the_socket(self):
        game = self.make_game(JustDanceGameV2)
        await game.on_message(json.dumps(menu_setup()))
        command = {"__class": "JD_Input_PhoneCommandData", "input": 185785632}
        await game.on_message(json.dumps(input_collection({"VALID": command})))
        sent = asyncio.Event()
        game.ws = SimpleNamespace(state=State.OPEN, send=AsyncMock(side_effect=lambda *args: sent.set()))
        game.is_connected = True
        game.controller.joycon.joycon_button_event("a", 1)
        task = asyncio.create_task(game.send_command())
        try:
            await asyncio.wait_for(sent.wait(), 1)
            game.ws.send.assert_awaited_once()
            self.assertEqual(json.loads(game.ws.send.await_args.args[0]), {"root": command})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_initial_carousel_permission_can_be_updated_independently(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for enabled in (None, False, True):
                with self.subTest(protocol=game_class.__name__, enabled=enabled):
                    game = self.make_game(game_class)
                    setup = menu_setup()
                    if enabled is not None:
                        setup["setupData"]["mainCarousel"]["isEnabled"] = enabled
                    await game.on_message(json.dumps(setup))
                    await self.message(game, "JD_PhoneUiShortcutData", shortcuts=[{
                        "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
                    }])
                    await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True)
                    self.assertEqual(game.is_input_allowed, enabled is not False)
                    self.assertEqual(game.is_command_allowed(Command.ACCEPT), enabled is not False)
                    self.assertTrue(game.is_command_allowed(Command.UPLOAD))
                    if enabled is False:
                        for cmd in (Command.ACCEPT, Command.RIGHT):
                            self.assertEqual(await game.preprocess_command(cmd), (None, None))
                    await self.message(game, "EnableCarousel_ConsoleCommandData", isEnabled=True)
                    self.assertTrue(game.is_command_allowed(Command.ACCEPT))

    async def test_disabled_or_unspecified_item_permission_blocks_validation(self):
        action = {"__class": "JD_Custom_PhoneCommandData", "identifier": "SELECT_ITEM"}
        for enabled in (None, False, True):
            for explicit_action in (False, True):
                with self.subTest(enabled=enabled, explicit_action=explicit_action):
                    game = self.make_game(JustDanceGameV1)
                    setup = menu_setup()
                    item = setup["setupData"]["mainCarousel"]["rows"][0]["items"][0]
                    item.pop("isEnabled")
                    if enabled is not None:
                        item["isEnabled"] = enabled
                    if explicit_action:
                        item["actions"] = [{"command": json.dumps({"root": action})}]
                    await game.on_message(json.dumps(setup))
                    self.assertEqual(game.is_command_allowed(Command.ACCEPT), enabled is True)
                    expected = (None, None)
                    if enabled:
                        expected = (action["__class"], {"identifier": "SELECT_ITEM"}) if explicit_action else (
                            "ValidateAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 0, "actionIndex": 0},
                        )
                    self.assertEqual(await game.preprocess_command(Command.ACCEPT), expected)

    async def test_item_permissions_follow_the_selected_row_and_column(self):
        game = self.make_game(JustDanceGameV1)
        setup = menu_setup()
        setup["setupData"]["mainCarousel"]["rows"][0]["items"][0]["isEnabled"] = False
        setup["setupData"]["mainCarousel"]["rows"][1]["items"][0]["isEnabled"] = False
        await game.on_message(json.dumps(setup))
        for direction, expected_allowed in (
            (Command.RIGHT, True), (Command.LEFT, False), (Command.DOWN, False),
        ):
            self.assertIsNotNone((await game.preprocess_command(direction))[0])
            self.assertEqual(game.is_command_allowed(Command.ACCEPT), expected_allowed)
        await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True,
                           carouselPosSetup={"rowIndex": 0, "itemIndex": 1, "actionIndex": 0})
        self.assertTrue(game.is_command_allowed(Command.ACCEPT))

    async def test_popup_and_replacement_setup_keep_item_permissions_scoped(self):
        for main_enabled in (False, True):
            with self.subTest(main_enabled=main_enabled):
                game = self.make_game(JustDanceGameV1)
                setup = menu_setup()
                setup["setupData"]["mainCarousel"]["rows"][0]["items"][0]["isEnabled"] = main_enabled
                await game.on_message(json.dumps(setup))
                popup = menu_setup()
                popup["isPopup"] = True
                popup["setupData"]["mainCarousel"]["rows"][0]["items"][0]["isEnabled"] = not main_enabled
                await game.on_message(json.dumps(popup))
                self.assertEqual(game.is_command_allowed(Command.ACCEPT), not main_enabled)
                await self.message(game, "JD_ClosePopup_ConsoleCommandData")
                self.assertEqual(game.is_command_allowed(Command.ACCEPT), main_enabled)
                await game.on_message(json.dumps(menu_setup()))
                self.assertTrue(game.is_command_allowed(Command.ACCEPT))

    async def test_locked_enabled_item_can_still_send_its_supplied_action(self):
        game = self.make_game(JustDanceGameV1)
        setup = menu_setup()
        item = setup["setupData"]["mainCarousel"]["rows"][0]["items"][0]
        item["isLocked"] = 1
        item["actions"] = [{"command": json.dumps({"root": {
            "__class": "JD_Custom_PhoneCommandData", "identifier": "OPEN_OFFER",
        }})}]
        await game.on_message(json.dumps(setup))
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (
            "JD_Custom_PhoneCommandData", {"identifier": "OPEN_OFFER"},
        ))

    async def test_disabled_item_confirmation_is_discarded_before_moving_to_enabled_item(self):
        game = self.make_game(JustDanceGameV1)
        setup = menu_setup()
        setup["setupData"]["mainCarousel"]["rows"][0]["items"][0]["isEnabled"] = False
        await game.on_message(json.dumps(setup))
        sent_times = await self.run_input_frames(game, [
            (0, "a", None), (0.05, None, Command.RIGHT), (0.1, None, None), (0.15, "a", None),
        ])
        self.assertEqual(sent_times, [0.05, 0.15])
        self.assertEqual([call.args for call in game.send_message.await_args_list], [
            ("ChangeItem_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1}),
            ("ValidateAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1, "actionIndex": 0}),
        ])

    async def test_navigation_and_shortcut_permissions_are_independent(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for navigation_message in ("InputSetup_ConsoleCommandData", "EnableCarousel_ConsoleCommandData"):
                with self.subTest(protocol=game_class.__name__, message=navigation_message):
                    game = self.make_game(game_class)
                    await game.on_message(json.dumps(menu_setup()))
                    await self.message(game, "JD_PhoneUiShortcutData", shortcuts=[{
                        "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
                    }])
                    await self.message(game, navigation_message, isEnabled=False)
                    await self.message(game, "ShortcutSetup_ConsoleCommandData", isEnabled=True)
                    self.assertFalse(game.is_input_allowed)
                    self.assertEqual(await game.preprocess_command(Command.RIGHT), (None, None))
                    self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
                    expected = (
                        (None, None) if navigation_message == "InputSetup_ConsoleCommandData" else (
                            "JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value},
                        )
                    )
                    self.assertEqual(await game.preprocess_command(Command.UPLOAD), expected)
                    other_message = (
                        "EnableCarousel_ConsoleCommandData" if navigation_message == "InputSetup_ConsoleCommandData"
                        else "InputSetup_ConsoleCommandData"
                    )
                    await self.message(game, other_message, isEnabled=True)
                    self.assertFalse(game.is_input_allowed)

                    await self.message(game, navigation_message, isEnabled=True)
                    await self.message(game, "ShortcutSetup_ConsoleCommandData", isEnabled=False)
                    self.assertTrue(game.is_input_allowed)
                    self.assertEqual(await game.preprocess_command(Command.UPLOAD), (None, None))
                    self.assertEqual(await game.preprocess_command(Command.BACK), (None, None))
                    self.assertIsNotNone((await game.preprocess_command(Command.RIGHT))[0])

                    # A new UI must not undo the separate shortcut disable message.
                    await game.on_message(json.dumps(menu_setup((Command.UPLOAD,))))
                    self.assertFalse(game.is_shortcuts_allowed)

    async def test_lobby_start_has_its_own_initial_and_updated_permission(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for start_button in (None, {"isEnabled": False}, {"isEnabled": True}):
                with self.subTest(protocol=game_class.__name__, start_button=start_button):
                    game = self.make_game(game_class)
                    lobby = {"coaches": [{}, {}]}
                    if start_button is not None:
                        lobby["startButton"] = {
                            **start_button, "actions": [{"command": json.dumps({
                                "root": {"__class": "JD_StartGame_PhoneCommandData"},
                            })}],
                        }
                    await self.message(game, "JD_PhoneUiSetupData", inputSetup={"isEnabled": True},
                                       setupData={"lobbySetup": lobby})
                    self.assertEqual(game.is_command_allowed(Command.ACCEPT), bool(start_button and start_button["isEnabled"]))
                    if not game.is_lobby_start_allowed:
                        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
                    await self.message(game, "JD_EnableLobbyStartbutton_ConsoleCommandData", isEnabled=False)
                    self.assertTrue(game.is_input_allowed)
                    self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
                    await self.message(game, "EnableCarousel_ConsoleCommandData", isEnabled=False)
                    await self.message(game, "JD_EnableLobbyStartbutton_ConsoleCommandData", isEnabled=True)
                    self.assertFalse(game.is_input_allowed)
                    self.assertEqual(game.is_command_allowed(Command.ACCEPT), start_button is not None)
                    await self.message(game, "ShortcutSetup_ConsoleCommandData", isEnabled=False)
                    self.assertEqual(game.is_command_allowed(Command.ACCEPT), start_button is not None)
                    await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=False)
                    self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))

    async def test_popup_permissions_and_shortcuts_restore_the_underlying_screen(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup((Command.UPLOAD,))))
                await self.message(game, "JD_PhoneUiShortcutData", shortcuts=[{
                    "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
                }])
                main_shortcuts = game.available_shortcuts.copy()
                popup = menu_setup()
                popup["isPopup"] = True
                popup["inputSetup"]["isEnabled"] = False
                await game.on_message(json.dumps(popup))
                self.assertFalse(game.is_input_allowed)
                self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))
                await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True, applyOnPopup=True)
                self.assertTrue(game.is_input_allowed)
                await self.message(game, "EnableCarousel_ConsoleCommandData", isEnabled=False, applyOnPopup=False)
                self.assertTrue(game.is_input_allowed)
                await self.message(game, "JD_ClosePopup_ConsoleCommandData")
                self.assertFalse(game.is_input_allowed)
                self.assertEqual(game.available_shortcuts, main_shortcuts)
                self.assertEqual(game.controller.available_shortcuts, main_shortcuts)

                await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True)
                self.assertFalse(game.is_input_allowed)
                await self.message(game, "EnableCarousel_ConsoleCommandData", isEnabled=True)
                await game.on_message(json.dumps(popup))
                await self.message(game, "JD_ClosePopup_ConsoleCommandData")
                self.assertTrue(game.is_input_allowed)

    async def test_popup_replacement_keeps_lobby_coach_and_start_context(self):
        game = self.make_game(JustDanceGameV1)
        setup = lobby_setup(selected_coach=1)
        setup["setupData"]["lobbySetup"]["startButton"] = {"isEnabled": False}
        await game.on_message(json.dumps(setup))
        for _ in range(2):
            popup = menu_setup()
            popup["isPopup"] = True
            await game.on_message(json.dumps(popup))
            self.assertFalse(game.is_in_lobby)
            self.assertEqual(await game.preprocess_command(Command.RIGHT), (
                "ChangeItem_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1},
            ))
        await self.message(game, "JD_ClosePopup_ConsoleCommandData")
        self.assertTrue(game.is_in_lobby)
        self.assertEqual(game.coach_id, 1)
        self.assertEqual(await game.preprocess_command(Command.RIGHT), (
            "JD_ChangeCoach_PhoneCommandData", {"coachId": 2},
        ))
        self.assertEqual(await game.preprocess_command(Command.ACCEPT), (None, None))

    async def test_shortcut_updates_target_the_main_screen_popup_or_active_layer(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup()))
                popup = menu_setup()
                popup["isPopup"] = True
                await game.on_message(json.dumps(popup))
                for target in ({}, {"applyOnPopup": True}):
                    await self.message(game, "JD_PhoneUiShortcutData", **target, shortcuts=[{
                        "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.SKIP.value,
                    }])
                    self.assertEqual(game.available_shortcuts, {Command.SKIP})
                await self.message(game, "JD_PhoneUiShortcutData", applyOnPopup=False, shortcuts=[{
                    "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
                }])
                self.assertEqual(game.available_shortcuts, {Command.SKIP})
                self.assertEqual(game.controller.available_shortcuts, {Command.SKIP})
                await self.message(game, "JD_ClosePopup_ConsoleCommandData")
                self.assertEqual(game.available_shortcuts, {Command.UPLOAD})
                self.assertEqual(game.controller.available_shortcuts, {Command.UPLOAD})

    async def test_position_updates_target_the_specified_carousel(self):
        game = self.make_game(JustDanceGameV1)
        await game.on_message(json.dumps(menu_setup()))
        popup = menu_setup()
        popup["isPopup"] = True
        await game.on_message(json.dumps(popup))
        await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=False, applyOnPopup=False,
                           carouselPosSetup={"rowIndex": 1, "itemIndex": 0, "actionIndex": 0})
        self.assertEqual(game.row_num, 0)
        self.assertTrue(game.is_input_allowed)
        await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True, applyOnPopup=True,
                           carouselPosSetup={"rowIndex": 0, "itemIndex": 1, "actionIndex": 0})
        self.assertEqual(game.col_num_per_row_id[0], 1)
        await self.message(game, "JD_ClosePopup_ConsoleCommandData")
        self.assertEqual(game.row_num, 1)
        self.assertFalse(game.is_input_allowed)

    async def test_replaced_keyboard_is_not_restored_on_popup_close(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup()))
                await self.message(game, "JD_OpenPhoneKeyboard_ConsoleCommandData")
                popup = menu_setup()
                popup["isPopup"] = True
                await game.on_message(json.dumps(popup))
                self.assertFalse(game.is_search_opened)
                await self.message(game, "JD_ClosePopup_ConsoleCommandData")
                self.assertFalse(game.is_search_opened)
                self.assertEqual(await game.preprocess_command(Command.BACK), (
                    "JD_Custom_PhoneCommandData", {"identifier": Command.BACK.value},
                ))

    async def test_keyboard_survives_partial_popup_input_updates(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await self.message(game, "JD_OpenPhoneKeyboard_ConsoleCommandData")
                await self.message(game, "JD_PhoneUiSetupData", isPopup=True, inputSetup={"isEnabled": False})
                self.assertTrue(game.is_search_opened)
                self.assertEqual(await game.preprocess_command(Command.BACK), (
                    "JD_CancelKeyboard_PhoneCommandData", {},
                ))

    async def test_controllers_cancel_keyboard_with_navigation_disabled(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for product_id, button in (
                (JOYCON_L_PRODUCT_ID, "down"), (JOYCON_R_PRODUCT_ID, "b"),
                (JOYCON_PRO_PRODUCT_ID, "b"), (JOYCON_PRO_PRODUCT_ID, "minus"),
            ):
                with self.subTest(protocol=game_class.__name__, controller=product_id, button=button):
                    game = self.make_game(game_class)
                    game.controller.product_id = product_id
                    game.controller.joycon.joycon_button_event(button, 1)
                    await self.message(game, "JD_OpenPhoneKeyboard_ConsoleCommandData")
                    self.assertEqual(game.controller.joycon._events_buffer, [])
                    await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=False)
                    await self.message(game, "ShortcutSetup_ConsoleCommandData", isEnabled=False)
                    if game_class is JustDanceGameV2:
                        await game.on_message(json.dumps(input_collection({})))
                    commands = {cmd for cmd in Command if game.is_command_allowed(cmd)}
                    self.assertEqual(commands, {Command.BACK})
                    game.should_start_accelerometer = True
                    self.assertFalse(game.is_command_allowed(Command.BACK))
                    game.should_start_accelerometer = False
                    read_buttons = game.controller.get_latest_command
                    await self.run_input_frames(game, [(0, button, None)])
                    game.send_message.assert_awaited_once_with("JD_CancelKeyboard_PhoneCommandData", {})
                    game.controller.get_latest_command = read_buttons
                    await self.message(game, "JD_CancelKeyboard_ConsoleCommandData")
                    self.assertFalse(game.is_search_opened)
                    self.assertFalse(game.is_command_allowed(Command.BACK))

    async def test_keyboard_submission_is_redacted_in_game_logs_but_sent_intact(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                game.ws = SimpleNamespace(state=State.OPEN, send=AsyncMock())
                with self.assertLogs("joydance", level="DEBUG") as logs:
                    await game.send_message("JD_SubmitKeyboard_PhoneCommandData", {"keyboardOutput": "private-test-input"})
                self.assertNotIn("private-test-input", "\n".join(logs.output))
                self.assertIn("JD_SubmitKeyboard_PhoneCommandData", "\n".join(logs.output))
                self.assertEqual(json.loads(game.ws.send.await_args.args[0])["root"]["keyboardOutput"], "private-test-input")

    async def test_permissions_are_independent_for_each_controller(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                first, second = self.make_game(game_class), self.make_game(game_class)
                for game in (first, second):
                    await game.on_message(json.dumps(menu_setup()))
                await self.message(first, "InputSetup_ConsoleCommandData", isEnabled=False)
                await self.message(first, "ShortcutSetup_ConsoleCommandData", isEnabled=False)
                self.assertFalse(first.is_input_allowed)
                self.assertFalse(first.is_shortcuts_allowed)
                self.assertTrue(second.is_input_allowed)
                self.assertTrue(second.is_shortcuts_allowed)

    async def test_disabled_and_pre_connection_presses_are_discarded(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                game.controller.joycon.joycon_button_event("a", 1)
                await game.on_message(json.dumps(menu_setup()))
                self.assertEqual(game.controller.joycon._events_buffer, [])
                await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=False)
                game.is_connected = True
                game.send_message = AsyncMock()
                task = asyncio.create_task(game.send_command())
                try:
                    game.controller.joycon.joycon_button_event("a", 1)
                    await asyncio.sleep(0.06)
                    self.assertEqual(game.controller.joycon._events_buffer, [])
                    # Also discard a press arriving between the last poll and the enable message.
                    game.controller.joycon.joycon_button_event("a", 1)
                    await self.message(game, "InputSetup_ConsoleCommandData", isEnabled=True)
                    await asyncio.sleep(0.06)
                    game.send_message.assert_not_awaited()
                    sent = asyncio.Event()
                    game.send_message.side_effect = lambda *args: sent.set()
                    game.controller.joycon.joycon_button_event("a", 1)
                    await asyncio.wait_for(sent.wait(), 1)
                    game.send_message.assert_awaited_once()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def run_input_frames(self, game, frames):
        frames = iter(frames)
        now = 0
        read_buttons = game.controller.get_latest_command

        def read_frame(commands_to_check):
            nonlocal now
            try:
                now, button, direction = next(frames)
            except StopIteration:
                raise asyncio.CancelledError from None
            if button:
                game.controller.joycon.joycon_button_event(button, 1)
                game.controller.joycon.joycon_button_event(button, 0)
            game.controller.get_joystick_command.return_value = direction
            return read_buttons(commands_to_check)

        game.controller.get_latest_command = Mock(side_effect=read_frame)
        game.is_connected = True
        sent_times = []
        game.send_message = AsyncMock(side_effect=lambda *args: sent_times.append(now))
        with patch("joydance.games.justdance_abstract.time", SimpleNamespace(monotonic=lambda: now)):
            with self.assertRaises(asyncio.CancelledError):
                await game.send_command()
        return sent_times

    async def test_rapid_button_presses_are_not_throttled(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            for product_id, button in ((JOYCON_R_PRODUCT_ID, "a"), (JOYCON_PRO_PRODUCT_ID, "right")):
                with self.subTest(protocol=game_class.__name__, button=button):
                    game = self.make_game(game_class)
                    game.controller.product_id = product_id
                    await game.on_message(json.dumps(menu_setup()))
                    sent_times = await self.run_input_frames(game, [
                        (0, button, None), (0.05, button, None), (0.1, button, None),
                        (0.15, None, None),
                    ])
                    self.assertEqual(sent_times, [0, 0.05, 0.1])
                    self.assertEqual(game.controller.joycon._events_buffer, [])

    async def test_button_presses_do_not_wait_for_or_extend_joystick_repeat_delay(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup()))
                sent_times = await self.run_input_frames(game, [
                    (0, None, Command.RIGHT),
                    (0.05, "a", Command.RIGHT),
                    (0.1, "a", Command.RIGHT),
                    (0.2, None, Command.RIGHT),
                    (0.24, None, Command.RIGHT),
                ])
                self.assertEqual(sent_times, [0, 0.05, 0.1, 0.24])
                if game_class is JustDanceGameV1:
                    expected = [
                        ("ChangeItem_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1}),
                        ("ValidateAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1, "actionIndex": 0}),
                        ("ValidateAction_PhoneCommandData", {"rowIndex": 0, "itemIndex": 1, "actionIndex": 0}),
                        ("ChangeItem_PhoneCommandData", {"rowIndex": 0, "itemIndex": 0}),
                    ]
                else:
                    expected = [("JD_Input_PhoneCommandData", {"input": cmd.value}) for cmd in (
                        Command.RIGHT, Command.ACCEPT, Command.ACCEPT, Command.RIGHT,
                    )]
                self.assertEqual([call.args for call in game.send_message.await_args_list], expected)

    async def test_held_joystick_movement_repeats_only_after_delay(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup()))
                sent_times = await self.run_input_frames(game, [
                    (now, None, Command.RIGHT) for now in (0, 0.1, 0.22, 0.24, 0.44, 0.48)
                ])
                self.assertEqual(sent_times, [0, 0.24, 0.48])

    async def test_joystick_release_and_direction_change_reset_repeat_delay(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup()))
                sent_times = await self.run_input_frames(game, [
                    (0, None, Command.RIGHT),
                    (0.05, "a", None),
                    (0.1, None, Command.RIGHT),
                    (0.15, None, Command.LEFT),
                    (0.2, None, Command.LEFT),
                ])
                self.assertEqual(sent_times, [0, 0.05, 0.1, 0.15])

    async def test_button_press_does_not_delay_first_joystick_movement(self):
        for game_class in (JustDanceGameV1, JustDanceGameV2):
            with self.subTest(protocol=game_class.__name__):
                game = self.make_game(game_class)
                await game.on_message(json.dumps(menu_setup()))
                sent_times = await self.run_input_frames(game, [
                    (0, "a", None), (0.05, None, Command.RIGHT), (0.1, None, Command.RIGHT),
                ])
                self.assertEqual(sent_times, [0, 0.05])


if __name__ == "__main__":
    unittest.main()
