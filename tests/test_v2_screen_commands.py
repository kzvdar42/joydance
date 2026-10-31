import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.protocol import State

from joydance.constants import Command
from joydance.controllers.joycon.pycon.constants import JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID
from joydance.games.justdance_v2 import JustDanceGameV2
from test_button_mapping import BufferedButtons, controller_with_buttons, input_collection, menu_setup, press


def descriptor(command):
    return {"command": json.dumps({"root": command})}


def screen(command, payload, popup=False):
    setup = menu_setup()
    setup["isPopup"] = popup
    data = setup["setupData"]
    if command == Command.UPLOAD:
        data["shortcuts"] = [{"shortcutType": command.value, **descriptor(payload)}]
    elif command == Command.BACK:
        data["navigationBar"] = [descriptor(payload)]
    elif command == Command.ACCEPT:
        data["lobbySetup"] = {"coaches": [], "startButton": {"isEnabled": True, "actions": [descriptor(payload)]}}
    elif command == Command.PAUSE:
        data["gameplaySetup"] = {"pauseSlider": {"actions": [descriptor(payload)]}}
    return setup


class V2ScreenCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_game(self):
        controller = controller_with_buttons()
        controller.get_joystick_command = Mock(return_value=None)
        controller.set_accel_collection = Mock()
        controller.set_player_led = AsyncMock()
        return JustDanceGameV2(controller, on_state_changed=AsyncMock(), on_game_message=AsyncMock())

    def setUp(self):
        self.game = self.make_game()

    async def message(self, name, **data):
        await self.game.on_message(json.dumps({"__class": name, **data}))

    async def test_all_four_buttons_send_the_complete_supplied_command_repeatedly(self):
        self.game.ws = SimpleNamespace(state=State.OPEN, send=AsyncMock())
        for button, command in (("y", Command.UPLOAD), ("b", Command.BACK), ("a", Command.ACCEPT), ("plus", Command.PAUSE)):
            with self.subTest(command=command):
                payload = {"__class": "JD_ScreenAction_PhoneCommandData", "targetId": 0,
                           "options": {"ids": [1, 2], "enabled": False}}
                await self.game.on_message(json.dumps(screen(command, payload)))
                for _ in range(2):
                    result = press(self.game.controller, button)
                    self.assertEqual(result, command)
                    cls, data = await self.game.preprocess_command(result)
                    await self.game.send_message(cls, data)
                    self.assertEqual(json.loads(self.game.ws.send.await_args.args[0]), {"root": payload})

    async def test_shortcut_messages_preserve_commands_and_update_the_addressed_layer(self):
        main = {"__class": "JD_Custom_PhoneCommandData", "identifier": "SEARCH_MAIN", "targetId": 42}
        popup = {**main, "targetId": 7}
        await self.game.on_message(json.dumps(screen(Command.UPLOAD, main)))
        await self.game.on_message(json.dumps(screen(Command.UPLOAD, popup, popup=True)))
        update = {"__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value, **descriptor(main)}
        await self.message("JD_PhoneUiShortcutData", applyOnPopup=False, shortcuts=[update])
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (popup["__class"], {"identifier": "SEARCH_MAIN", "targetId": 7}))
        await self.message("JD_ClosePopup_ConsoleCommandData")
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (main["__class"], {"identifier": "SEARCH_MAIN", "targetId": 42}))

    async def test_popup_close_restores_each_underlying_button_command(self):
        for command in (Command.UPLOAD, Command.BACK, Command.ACCEPT, Command.PAUSE):
            with self.subTest(command=command):
                payload = {"__class": "JD_ScreenAction_PhoneCommandData", "targetId": 42}
                await self.game.on_message(json.dumps(screen(command, payload)))
                await self.game.on_message(json.dumps(screen(command, {**payload, "targetId": 7}, popup=True)))
                self.assertEqual(await self.game.preprocess_command(command), (payload["__class"], {"targetId": 7}))
                await self.message("JD_ClosePopup_ConsoleCommandData")
                self.assertEqual(await self.game.preprocess_command(command), (payload["__class"], {"targetId": 42}))

    async def test_partial_ui_updates_preserve_commands_and_input_permissions(self):
        for command in (Command.UPLOAD, Command.BACK, Command.ACCEPT, Command.PAUSE):
            with self.subTest(command=command):
                payload = {"__class": "JD_ScreenAction_PhoneCommandData", "targetId": 42}
                await self.game.on_message(json.dumps(screen(command, payload)))
                for update in ({}, {"disabledSetupData": {}}, {"setupData": None}):
                    await self.message("JD_PhoneUiSetupData", **update)
                    self.assertEqual(await self.game.preprocess_command(command), (payload["__class"], {"targetId": 42}))
                await self.message("JD_PhoneUiSetupData", inputSetup={"isEnabled": False})
                self.assertEqual(await self.game.preprocess_command(command), (None, None))

    async def test_navigation_updates_do_not_replace_the_lobby_start_or_pause_action(self):
        for command in (Command.ACCEPT, Command.PAUSE):
            payload = {"__class": "JD_ScreenAction_PhoneCommandData", "targetId": 42}
            await self.game.on_message(json.dumps(screen(command, payload)))
            await self.message("JD_PhoneUiShortcutData", shortcuts=[])
            self.assertEqual(await self.game.preprocess_command(command), (payload["__class"], {"targetId": 42}))

    async def test_start_uses_its_own_action_even_when_global_valid_is_unavailable(self):
        payload = {"__class": "JD_StartGame_PhoneCommandData", "targetId": 42}
        await self.game.on_message(json.dumps(screen(Command.ACCEPT, payload)))
        await self.game.on_message(json.dumps(input_collection({})))
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (payload["__class"], {"targetId": 42}))
        await self.message("JD_EnableLobbyStartbutton_ConsoleCommandData", isEnabled=False)
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (None, None))

    async def test_keyboard_cancel_takes_priority_over_a_supplied_back_command(self):
        await self.game.on_message(json.dumps(screen(Command.BACK, {"__class": "JD_GoBack_PhoneCommandData", "targetId": 42})))
        await self.message("JD_OpenPhoneKeyboard_ConsoleCommandData")
        await self.message("InputSetup_ConsoleCommandData", isEnabled=False)
        self.assertEqual(await self.game.preprocess_command(Command.BACK), ("JD_CancelKeyboard_PhoneCommandData", {}))

    async def test_pause_keeps_its_supplied_action_while_scoring_is_active(self):
        payload = {"__class": "JD_Pause_PhoneCommandData", "targetId": 42}
        await self.game.on_message(json.dumps(screen(Command.PAUSE, payload)))
        await self.message("JD_EnableAccelValuesSending_ConsoleCommandData")
        self.assertEqual(await self.game.preprocess_command(Command.PAUSE), (payload["__class"], {"targetId": 42}))
        self.assertEqual(await self.game.preprocess_command(Command.ACCEPT), (None, None))

    async def test_empty_or_invalid_supplied_actions_do_not_send_a_fallback(self):
        for command in (Command.UPLOAD, Command.BACK, Command.ACCEPT, Command.PAUSE):
            for encoded in (None, "", "not json", "null", "[]", "{}", '{"root":[]}', '{"root":{"__class":42}}'):
                with self.subTest(command=command, encoded=encoded):
                    setup = screen(command, {"__class": "JD_Action_PhoneCommandData"})
                    data = setup["setupData"]
                    target = (
                        data["shortcuts"][0] if command == Command.UPLOAD else
                        data["navigationBar"][0] if command == Command.BACK else
                        data["lobbySetup"]["startButton"]["actions"][0] if command == Command.ACCEPT else
                        data["gameplaySetup"]["pauseSlider"]["actions"][0]
                    )
                    target["command"] = encoded
                    with patch("joydance.games.justdance_abstract.logger"):
                        await self.game.on_message(json.dumps(setup))
                    self.assertEqual(await self.game.preprocess_command(command), (None, None))

    async def test_empty_action_lists_and_navigation_bar_clear_previous_actions(self):
        for command in (Command.BACK, Command.ACCEPT, Command.PAUSE):
            setup = screen(command, {"__class": "JD_Action_PhoneCommandData"})
            await self.game.on_message(json.dumps(setup))
            if command == Command.BACK:
                setup["setupData"]["navigationBar"] = []
            else:
                target = setup["setupData"]["lobbySetup"]["startButton"] if command == Command.ACCEPT else setup["setupData"]["gameplaySetup"]["pauseSlider"]
                target["actions"] = []
            await self.game.on_message(json.dumps(setup))
            self.assertEqual(await self.game.preprocess_command(command), (None, None))

    async def test_direct_button_commands_match_the_melody_descriptor_format(self):
        for command in (Command.ACCEPT, Command.PAUSE):
            payload = {"__class": "JD_ScreenAction_PhoneCommandData", "targetId": 42}
            setup = screen(command, payload)
            target = setup["setupData"]["lobbySetup"]["startButton"] if command == Command.ACCEPT else setup["setupData"]["gameplaySetup"]["pauseSlider"]
            target.pop("actions")
            target.update(descriptor(payload))
            await self.game.on_message(json.dumps(setup))
            self.assertEqual(await self.game.preprocess_command(command), (payload["__class"], {"targetId": 42}))

    async def test_start_and_pause_use_the_first_action_without_falling_through(self):
        for command in (Command.ACCEPT, Command.PAUSE):
            payload = {"__class": "JD_FirstAction_PhoneCommandData", "targetId": 0}
            setup = screen(command, payload)
            target = setup["setupData"]["lobbySetup"]["startButton"] if command == Command.ACCEPT else setup["setupData"]["gameplaySetup"]["pauseSlider"]
            target["actions"].append(descriptor({"__class": "JD_SecondAction_PhoneCommandData"}))
            await self.game.on_message(json.dumps(setup))
            self.assertEqual(await self.game.preprocess_command(command), (payload["__class"], {"targetId": 0}))
            target["actions"][0]["command"] = ""
            await self.game.on_message(json.dumps(setup))
            self.assertEqual(await self.game.preprocess_command(command), (None, None))

    async def test_replacing_navigation_discards_queued_presses(self):
        await self.game.on_message(json.dumps(menu_setup()))
        self.game.controller.joycon = BufferedButtons()
        self.game.controller.joycon.joycon_button_event("y", 1)
        update = {"__class": "JD_PhoneUiShortcutData", "shortcuts": [{
            "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
            **descriptor({"__class": "JD_Custom_PhoneCommandData", "identifier": "UPLOAD", "targetId": 42}),
        }]}
        self.game._on_game_message.reset_mock()
        await self.game.on_message(json.dumps(update))
        self.assertEqual(self.game.controller.joycon._events_buffer, [])
        self.assertEqual(self.game.controller.available_shortcuts, {Command.UPLOAD})
        self.game._on_game_message.assert_awaited_once_with(update)

    async def test_shortcut_identifier_can_come_from_the_command_when_type_is_missing(self):
        payload = {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.UPLOAD.value, "targetId": 42}
        setup = screen(Command.UPLOAD, payload)
        setup["setupData"]["shortcuts"][0].pop("shortcutType")
        await self.game.on_message(json.dumps(setup))
        self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (payload["__class"], {"identifier": Command.UPLOAD.value, "targetId": 42}))

    async def test_new_screen_and_disconnect_do_not_reuse_old_payloads(self):
        payload = {"__class": "JD_Custom_PhoneCommandData", "identifier": Command.UPLOAD.value, "targetId": 42}
        for reset in ("screen", "disconnect"):
            await self.game.on_message(json.dumps(screen(Command.UPLOAD, payload)))
            if reset == "disconnect":
                await self.game.disconnect(should_reconnect=False)
            await self.game.on_message(json.dumps(menu_setup()))
            self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), (None, None))
            await self.message("JD_PhoneUiShortcutData", shortcuts=[{
                "__class": "JD_PhoneAction_Shortcut", "shortcutType": Command.UPLOAD.value,
            }])
            self.assertEqual(await self.game.preprocess_command(Command.UPLOAD), ("JD_Custom_PhoneCommandData", {"identifier": Command.UPLOAD.value}))

    async def test_supplied_commands_are_independent_between_controllers(self):
        other = self.make_game()
        for game, target in ((self.game, 7), (other, 42)):
            await game.on_message(json.dumps(screen(Command.BACK, {"__class": "JD_GoBack_PhoneCommandData", "targetId": target})))
        self.assertEqual(await self.game.preprocess_command(Command.BACK), ("JD_GoBack_PhoneCommandData", {"targetId": 7}))
        self.assertEqual(await other.preprocess_command(Command.BACK), ("JD_GoBack_PhoneCommandData", {"targetId": 42}))

    async def test_jdmo_video_controls_can_coexist_and_still_pause_while_scoring(self):
        for product_id, buttons in (
            (JOYCON_R_PRODUCT_ID, ("a", "x", "y", "plus", "r", "zr", "stick_r_btn")),
            (JOYCON_L_PRODUCT_ID, ("right", "up", "left", "minus", "l", "zl", "stick_l_btn")),
            (JOYCON_PRO_PRODUCT_ID, ("a", "x", "y", "plus", "l", "r", "stick_r_btn")),
        ):
            with self.subTest(product_id=product_id):
                self.game = self.make_game()
                self.game.controller.product_id = product_id
                identifiers = (
                    "SHORTCUT_VALIDATE", "SHORTCUT_TOGGLE_SUBTITLES", "SHORTCUT_VIDEO_RESTART",
                    "SHORTCUT_TOGGLE_VIDEO_PLAYBACK", "SHORTCUT_TOGGLE_RUMBLE",
                    "SHORTCUT_TOGGLE_RELATED_SONGS_AUTOPLAY", "SHORTCUT_TOGGLE_AUTOSCROLLING",
                )
                setup = menu_setup()
                setup["setupData"]["shortcuts"] = [
                    {"shortcutType": identifier, **descriptor({
                        "__class": "JD_ScreenAction_PhoneCommandData", "action": identifier, "targetId": 0,
                    })}
                    for identifier in identifiers
                ]
                await self.game.on_message(json.dumps(setup))
                for button, identifier in zip(buttons, identifiers):
                    command = Command(identifier)
                    self.assertEqual(press(self.game.controller, button), command)
                    self.assertEqual(await self.game.preprocess_command(command), (
                        "JD_ScreenAction_PhoneCommandData", {"action": identifier, "targetId": 0},
                    ))
                await self.message("ShortcutSetup_ConsoleCommandData", isEnabled=False)
                for identifier in identifiers:
                    self.assertEqual(await self.game.preprocess_command(Command(identifier)), (None, None))
                await self.message("JD_EnableAccelValuesSending_ConsoleCommandData")
                allowed = {cmd for cmd in Command if self.game.is_command_allowed(cmd)}
                self.assertEqual(press(self.game.controller, buttons[3], allowed), Command.PAUSE)
