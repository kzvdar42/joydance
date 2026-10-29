import asyncio
from collections import defaultdict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dance
from joydance.config_handler import ConfigHandler
from joydance.constants import WsCommand
from joydance.games.justdance_v2 import JustDanceGameV2


class BrowserUpdateTests(unittest.IsolatedAsyncioTestCase):
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
