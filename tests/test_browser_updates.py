import asyncio
from collections import defaultdict
from functools import partial
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dance
from joydance.config_handler import ConfigHandler
from joydance.constants import WsCommand
from joydance.controllers.joycon.joycon_wrapper import JoyConWrapper
from joydance.controllers.joycon.pycon.constants import JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID
from joydance.games.v2.game import JustDanceGameV2
from joydance.games.jdnow import web as jdnow_web
from joydance.games.jdnow.game import JustDanceGameNow


class BrowserUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_rumble_can_be_toggled_before_pairing_and_keeps_state_on_refresh(self):
        controller = JoyConWrapper(JOYCON_VENDOR_ID, JOYCON_L_PRODUCT_ID, "rumble-test")
        self.addCleanup(controller.close)
        controller.battery_level = AsyncMock(return_value=4)
        app = web.Application()
        app.update({"controllers": {controller.serial: controller}, "joydance_connections": {},
                    "controllers_info": {controller.serial: {"serial": controller.serial, "state": 0}},
                    "websockets": set()})
        app.router.add_get("/ws", dance.websocket_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        browser = await client.ws_connect("/ws")

        async def toggle(enabled):
            await browser.send_json({"cmd": "toggle_rumble", "data": {
                "controller_serial": controller.serial, "enabled": enabled,
            }})
            response = await browser.receive_json(timeout=1)
            self.assertEqual(response["cmd"], "resp_update_controller_state")
            self.assertEqual(response["data"]["rumble_enabled"], enabled)
            self.assertEqual((await controller.get_state())["rumble_enabled"], enabled)
            self.assertEqual(controller.joycon.rumble_enabled, enabled)

        await toggle(False)
        game = JustDanceGameV2(controller)
        app["joydance_connections"][controller.serial] = game
        self.assertFalse((await game.get_state())["rumble_enabled"])
        await toggle(True)
        self.assertTrue((await game.get_state(False))["rumble_enabled"])

    async def test_jdnow_sign_in_only_imports_and_broadcasts_from_the_local_ui(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = ConfigHandler(str(Path(directory.name) / "config.cfg"))
        app = web.Application()
        app.update({"config_handler": config, "joydance_connections": {}, "websockets": set(),
                    "controllers_info": {"left": {"serial": "left", "state": 0}},
                    "jdnow_sign_in_lock": asyncio.Lock(), "jdnow_guest_lock": asyncio.Lock()})
        app_path = str(Path(dance.__file__).resolve())
        app.router.add_post("/jdnow/sign-in", partial(
            jdnow_web.sign_in_handler, port=dance.WEB_PORT, app_path=app_path, broadcast=dance.ws_broadcast_response,
        ))
        app.router.add_get("/ws", dance.websocket_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        browser = await client.ws_connect("/ws")
        host = f"localhost:{dance.WEB_PORT}"
        private_id = "private-linked-profile-1234"

        def open_window_stub(_):
            self.assertTrue(app["jdnow_sign_in_lock"].locked())
            self.assertFalse(app["jdnow_guest_lock"].locked())
            return "test-ubisoft-id"

        def recover_stub(_):
            self.assertTrue(app["jdnow_sign_in_lock"].locked())
            self.assertTrue(app["jdnow_guest_lock"].locked())
            return private_id, "Linked Dancer"

        with (
            patch.object(jdnow_web, "open_sign_in_window", side_effect=open_window_stub) as open_window,
            patch.object(jdnow_web, "recover_profile", side_effect=recover_stub) as recover,
        ):
            async with client.post("/jdnow/sign-in", headers={"Host": host, "Origin": "https://example.com"}) as response:
                self.assertEqual(response.status, 403)
            open_window.assert_not_awaited()
            async with client.post("/jdnow/sign-in", headers={"Host": host, "Origin": f"http://{host}"}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), {})
            open_window.assert_awaited_once_with(app_path)
            recover.assert_awaited_once_with("test-ubisoft-id")
        message = await browser.receive_json(timeout=1)
        self.assertEqual(message["cmd"], "resp_update_controller_state")
        linked, = message["data"]["jdnow_guests"]
        self.assertEqual(linked["name"], "Ubisoft profile 1 · Linked Dancer")
        self.assertEqual(config.get_jdnow_player_id("left", linked["key"]), private_id)
        self.assertNotIn(private_id, str(message))

    async def test_jdnow_web_actions_target_only_the_selected_controller(self):
        first, second = [Mock(spec=JustDanceGameNow, search_songs=AsyncMock(), select_song=AsyncMock(),
                              rename_player=AsyncMock(), on_state_changed=AsyncMock()) for _ in range(2)]
        app = web.Application()
        app.update({"websockets": set(), "joydance_connections": {"first": first, "second": second},
                    "config_handler": SimpleNamespace(connections=[])})
        app.router.add_get("/ws", dance.websocket_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        browser = await client.ws_connect("/ws")
        for command, field, value, handler in (
            ("search_input", "text", "Magic", second.search_songs),
            ("select_song", "song", "24K", second.select_song),
            ("rename_player", "name", "New Dancer", second.rename_player),
        ):
            await browser.send_json({"cmd": command, "data": {"controller_serial": "second", field: value}})
            if command == "rename_player":
                self.assertEqual(await browser.receive_json(timeout=1), {
                    "cmd": "resp_rename_player", "data": {"serial": "second", "submitted_name": value},
                })
            await browser.send_json({"cmd": "get_connections"})
            self.assertEqual((await browser.receive_json(timeout=1))["cmd"], "resp_get_connections")
            handler.assert_awaited_once_with(value)
        for handler in (first.search_songs, first.select_song, first.rename_player):
            handler.assert_not_awaited()
        second.rename_player.side_effect = ValueError("Invalid player name.")
        await browser.send_json({"cmd": "rename_player", "data": {"controller_serial": "second", "name": ""}})
        await browser.send_json({"cmd": "get_connections"})
        await browser.receive_json(timeout=1)
        second.on_state_changed.assert_awaited_once_with({"player_rename_feedback": {"text": "Invalid player name.", "error": True}})

        second.on_state_changed.reset_mock()
        second.select_song.side_effect = ValueError("Unknown song.")
        await browser.send_json({"cmd": "select_song", "data": {"controller_serial": "second", "song": "Unknown"}})
        await browser.send_json({"cmd": "get_connections"})
        await browser.receive_json(timeout=1)
        second.on_state_changed.assert_awaited_once_with({"song_search_feedback": {"text": "Unknown song.", "error": True}})

    async def test_keyboard_replies_target_the_requesting_controller_from_either_browser(self):
        first = SimpleNamespace(is_connected=True, is_search_opened=True, send_message=AsyncMock())
        second = SimpleNamespace(is_connected=True, is_search_opened=True, send_message=AsyncMock())
        app = web.Application()
        app.update({"websockets": set(), "joydance_connections": {"first": first, "second": second}})
        app.router.add_get("/ws", dance.websocket_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        browsers = [await client.ws_connect("/ws"), await client.ws_connect("/ws")]

        cases = (
            ("second", True, True, second),
            ("first", True, True, first),
            ("unknown", True, True, None),
            (None, True, True, None),
            ([], True, True, None),
            ("second", False, True, None),
            ("second", True, False, None),
        )
        with (
            patch.object(dance, "update_controllers_info", new=AsyncMock(return_value=[])),
            self.assertLogs("dance", level="DEBUG") as logs,
        ):
            for browser in browsers:
                for serial, keyboard_open, connected, target in cases:
                    with self.subTest(browser=browsers.index(browser), serial=serial, keyboard_open=keyboard_open, connected=connected):
                        first.send_message.reset_mock()
                        second.send_message.reset_mock()
                        second.is_search_opened = keyboard_open
                        second.is_connected = connected
                        data = {"text": "Test song"}
                        if serial is not None:
                            data["controller_serial"] = serial
                        await browser.send_json({"cmd": "search_input", "data": data})
                        # The following response confirms the preceding input has been handled.
                        await browser.send_json({"cmd": "get_controller_list"})
                        self.assertEqual((await browser.receive_json(timeout=1))["cmd"], "resp_get_controller_list")
                        for game in (first, second):
                            if game is target:
                                game.send_message.assert_awaited_once_with(
                                    "JD_SubmitKeyboard_PhoneCommandData", {"keyboardOutput": "Test song"},
                                )
                            else:
                                game.send_message.assert_not_awaited()
            await browsers[0].send_json({"cmd": "invalid", "data": {"text": "Test song"}})
            await browsers[0].send_str('{"cmd":"search_input","data":{"text":"Test song",invalid}}')
            await browsers[0].send_json({"cmd": "get_controller_list"})
            self.assertEqual((await browsers[0].receive_json(timeout=1))["cmd"], "resp_get_controller_list")
        self.assertNotIn("Test song", "\n".join(logs.output))

    async def test_existing_game_delivers_state_keyboard_and_rumble_after_browser_reconnect(self):
        controller = SimpleNamespace(
            serial="test", set_accel_collection=Mock(), set_player_led=AsyncMock(), set_rumble=Mock(),
            animate_connecting_led=AsyncMock(),
        )
        app = web.Application()
        app.update({
            "websockets": set(), "controllers": {"test": controller},
            "controllers_info": defaultdict(dict, {"test": {"serial": "test"}}),
            "joydance_connections": {}, "joydance_connection_locks": defaultdict(asyncio.Lock),
            "config_handler": SimpleNamespace(data={}, validate_config_with_error=ConfigHandler.validate_config_with_error),
        })
        app.router.add_get("/ws", dance.websocket_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        original = await client.ws_connect("/ws")
        with patch.object(JustDanceGameV2, "pair", new=asyncio.Event().wait):
            await dance.connect_controller(app, next(iter(app["websockets"])), {
                "controller_serial": "test", "pairing_method": "fast",
                "host_ip_addr": "192.168.1.2", "console_ip_addr": "192.168.1.3", "pairing_code": "123456",
            })
            game = app["joydance_connections"]["test"]
            self.addAsyncCleanup(game.disconnect, should_reconnect=False)
            await original.close()
            replacement = await client.ws_connect("/ws")
            another_browser = await client.ws_connect("/ws")

            await game.on_state_changed({"state": 4})
            for browser in (replacement, another_browser):
                response = await browser.receive_json(timeout=1)
                self.assertEqual(response["cmd"], "resp_update_controller_state")
                self.assertEqual(response["data"]["state"], 4)

            for message_class, command in (
                ("JD_OpenPhoneKeyboard_ConsoleCommandData", "resp_show_search"),
                ("JD_CancelKeyboard_ConsoleCommandData", "resp_hide_search"),
            ):
                await game.on_game_message({"__class": message_class})
                for browser in (replacement, another_browser):
                    expected = {"serial": "test"}
                    if command == "resp_show_search":
                        expected["isPassword"] = False
                    self.assertEqual(await browser.receive_json(timeout=1), {"cmd": command, "data": expected})

            await replacement.send_json({"cmd": "toggle_rumble", "data": {"controller_serial": "test", "enabled": False}})
            for browser in (replacement, another_browser):
                response = await browser.receive_json(timeout=1)
                self.assertFalse(response["data"]["rumble_enabled"])
            controller.set_rumble.assert_called_once_with(False)
            await replacement.close()
            await another_browser.close()
            # Wait for both server handlers to finish their registry cleanup.
            async def wait_until_removed():
                while app["websockets"]:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_until_removed(), 1)
            self.assertEqual(app["websockets"], set())

    async def test_closed_or_failing_browser_does_not_block_other_updates(self):
        closed = Mock(closed=True, send_json=AsyncMock())
        failing = Mock(closed=False, send_json=AsyncMock(side_effect=ConnectionError("Browser left")))
        healthy = Mock(closed=False, send_json=AsyncMock())
        with self.assertLogs("dance", level="ERROR"):
            await dance.ws_broadcast_response(
                {"websockets": {closed, failing, healthy}}, WsCommand.UPDATE_CONTROLLER_STATE, {"serial": "test", "state": 4},
            )
        closed.send_json.assert_not_awaited()
        healthy.send_json.assert_awaited_once_with({"cmd": "resp_update_controller_state", "data": {"serial": "test", "state": 4}})
