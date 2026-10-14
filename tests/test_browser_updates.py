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
    async def test_existing_game_delivers_state_keyboard_and_rumble_after_browser_reconnect(self):
        controller = SimpleNamespace(serial="test", set_player_led=AsyncMock(), set_rumble=Mock())
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

            await game.on_state_changed("test", {"state": 4})
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
                    self.assertEqual(await browser.receive_json(timeout=1), {"cmd": command, "data": {"serial": "test"}})

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
