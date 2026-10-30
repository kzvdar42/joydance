import asyncio
from collections import defaultdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import dance
from joydance.config_handler import ConfigHandler
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2
from joydance.utils import GameConnectionError
from websockets.protocol import State


class SessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = SimpleNamespace(
            serial="test", is_connected=Mock(return_value=True),
            set_accel_collection=Mock(),
            set_player_led=AsyncMock(),
            animate_connecting_led=AsyncMock(),
        )
        self.app = {
            "controllers": {"test": self.controller},
            "controllers_info": defaultdict(dict),
            "joydance_connections": {},
            "websockets": set(),
            "joydance_connection_locks": defaultdict(asyncio.Lock),
            "config_handler": SimpleNamespace(
                data={}, validate_config_with_error=ConfigHandler.validate_config_with_error,
            ),
        }
        self.ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        self.data = {
            "controller_serial": "test", "pairing_method": "fast",
            "host_ip_addr": "192.168.1.2", "console_ip_addr": "192.168.1.3",
            "pairing_code": "123456",
        }
        self.sessions = []
        for name, game_class in (("JustDanceGameV1", JustDanceGameV1), ("JustDanceGameV2", JustDanceGameV2)):
            def create(game_class=game_class, **kwargs):
                game = game_class(**kwargs)
                game.pair = asyncio.Event().wait
                self.sessions.append(game)
                return game
            factory = patch.object(dance, name, new=create)
            factory.start()
            self.addCleanup(factory.stop)
        self.addAsyncCleanup(self.stop_all_sessions)

    async def stop_all_sessions(self):
        for game in self.sessions:
            await game.disconnect(should_reconnect=False)

    async def test_popup_close_and_disconnect_hide_keyboard_in_all_browsers(self):
        self.controller.get_latest_command = Mock(return_value=None)
        browsers = [Mock(closed=False, send_json=AsyncMock()) for _ in range(2)]
        self.app["websockets"].update(browsers)
        for method in ("old", "fast"):
            for reason in ("popup", "replacement", "manual_disconnect", "connection_loss", "connection_error"):
                with self.subTest(protocol=method, reason=reason):
                    data = {**self.data, "pairing_method": method}
                    await dance.connect_controller(self.app, self.ws, data)
                    game = self.app["joydance_connections"]["test"]
                    game.is_connected = True
                    for browser in browsers:
                        browser.send_json.reset_mock()
                    await game.on_message(json.dumps({"__class": "JD_OpenPhoneKeyboard_ConsoleCommandData", "isPassword": True}))
                    for browser in browsers:
                        browser.send_json.assert_awaited_once_with({
                            "cmd": "resp_show_search", "data": {"serial": "test", "isPassword": True},
                        })
                        browser.send_json.reset_mock()

                    with patch.object(game, "attempt_reconnect", new=asyncio.Event().wait):
                        if reason == "popup":
                            await game.on_message(json.dumps({"__class": "JD_ClosePopup_ConsoleCommandData"}))
                        elif reason == "replacement":
                            await game.on_message(json.dumps({
                                "__class": "JD_PhoneUiSetupData", "isPopup": True,
                                "inputSetup": {"isEnabled": False},
                            }))
                            self.assertTrue(game.is_search_opened)
                            for browser in browsers:
                                browser.send_json.assert_not_awaited()
                            await game.on_message(json.dumps({
                                "__class": "JD_PhoneUiSetupData", "isPopup": True,
                                "setupData": {"__class": "JD_GenericPhoneUiData"},
                            }))
                        elif reason == "manual_disconnect":
                            await dance.disconnect_controller(self.app, self.ws, data)
                        elif reason == "connection_error":
                            await game._handle_connection_error(GameConnectionError("Game connection timed out"))
                        else:
                            await game.disconnect(should_reconnect=True)
                        self.assertFalse(game.is_search_opened)
                        for browser in browsers:
                            keyboard_updates = [
                                call.args[0] for call in browser.send_json.await_args_list
                                if call.args[0]["cmd"] == "resp_hide_search"
                            ]
                            self.assertEqual(keyboard_updates, [{
                                "cmd": "resp_hide_search", "data": {"serial": "test"},
                            }])

    async def test_replacement_and_stop_leave_no_old_pairing_or_retry_tasks(self):
        for method in ("old", "fast"):
            with self.subTest(protocol=method):
                data = {**self.data, "pairing_method": method}
                await dance.connect_controller(self.app, self.ws, data)
                first = self.app["joydance_connections"]["test"]
                first_pair = first.pair_task
                retry = asyncio.create_task(asyncio.Event().wait())
                first.reconnection_task = retry
                first.ws = SimpleNamespace(state=State.OPEN, close=AsyncMock())
                old_ws = first.ws
                first.is_connected = True
                await asyncio.sleep(0)

                await dance.connect_controller(self.app, self.ws, data)
                second = self.app["joydance_connections"]["test"]
                second_pair = second.pair_task
                await asyncio.sleep(0)
                await dance.disconnect_controller(self.app, self.ws, data)

                self.assertIsNot(first, second)
                self.assertTrue(first_pair.cancelled())
                self.assertTrue(retry.cancelled())
                self.assertTrue(second_pair.cancelled())
                old_ws.close.assert_awaited_once()
                for game in (first, second):
                    self.assertFalse(game.should_reconnect)
                    self.assertIsNone(game.pair_task)
                    self.assertIsNone(game.reconnection_task)

    async def test_overlapping_connects_and_stop_wait_for_previous_socket_cleanup(self):
        await dance.connect_controller(self.app, self.ws, self.data)
        first = self.app["joydance_connections"]["test"]
        close_started, close_release = asyncio.Event(), asyncio.Event()

        async def close():
            close_started.set()
            await close_release.wait()

        first.ws = SimpleNamespace(state=State.OPEN, close=close)
        first.is_connected = True
        tasks = [asyncio.create_task(dance.connect_controller(self.app, self.ws, self.data))]
        try:
            await asyncio.wait_for(close_started.wait(), 1)
            tasks.append(asyncio.create_task(dance.connect_controller(self.app, self.ws, self.data)))
            tasks.append(asyncio.create_task(dance.disconnect_controller(self.app, self.ws, self.data)))
            await asyncio.sleep(0)
            self.assertIs(self.app["joydance_connections"]["test"], first)
            self.assertTrue(all(not task.done() for task in tasks))
        finally:
            close_release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
        for game in self.sessions:
            self.assertFalse(game.should_reconnect)
            self.assertIsNone(game.pair_task)
            self.assertIsNone(game.ws)

    async def test_invalid_replacement_keeps_current_session_running(self):
        await dance.connect_controller(self.app, self.ws, self.data)
        first = self.app["joydance_connections"]["test"]
        with self.assertLogs("dance", level="ERROR"):
            with self.assertRaises(ValueError):
                await dance.connect_controller(
                    self.app, self.ws, {**self.data, "pairing_method": "invalid"},
                )
        self.assertIs(self.app["joydance_connections"]["test"], first)
        self.assertFalse(first.pair_task.done())
