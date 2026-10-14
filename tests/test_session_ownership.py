import asyncio
from collections import defaultdict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import dance
from joydance.config_handler import ConfigHandler
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2
from websockets.protocol import State


class SessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = SimpleNamespace(
            serial="test", is_connected=Mock(return_value=True),
            set_player_led=AsyncMock(),
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
