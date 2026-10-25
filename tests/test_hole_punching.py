import asyncio
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from joydance.constants import PairingState
from joydance.games.justdance_v2 import JustDanceGameV2


class HolePunchingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        controller = SimpleNamespace(
            serial="hole-punch-test", is_connected=Mock(return_value=True),
            set_accel_collection=Mock(),
            set_player_led=AsyncMock(),
            animate_connecting_led=AsyncMock(),
        )
        self.state_changed = AsyncMock()
        self.game = JustDanceGameV2(controller, on_state_changed=self.state_changed)
        self.game.host_port = 0
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        socket_module = SimpleNamespace(
            socket=lambda *args: self.listener, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
        )
        factory = patch("joydance.games.abstract_game_wrapper.socket", socket_module)
        factory.start()
        self.addCleanup(factory.stop)
        self.addAsyncCleanup(self.game.disconnect, should_reconnect=False)

    async def test_accept_yields_to_other_tasks_and_disconnect_closes_accepted_socket(self):
        task = asyncio.create_task(self.game.hole_punching())
        self.addAsyncCleanup(self.cancel_task, task)
        # A separate coroutine must run while hole punching awaits the console.
        await asyncio.wait_for(asyncio.sleep(0), 1)
        self.assertFalse(task.done())
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.setblocking(False)
        await asyncio.get_running_loop().sock_connect(client, ("127.0.0.1", self.listener.getsockname()[1]))
        await asyncio.wait_for(task, 1)
        accepted = self.game.console_conn
        self.assertEqual(accepted.gettimeout(), 0)
        self.assertEqual(self.listener.fileno(), -1)
        self.state_changed.assert_not_awaited()
        await self.game.disconnect(should_reconnect=False)
        self.assertEqual(accepted.fileno(), -1)
        self.assertIsNone(self.game.console_conn)

    async def test_timeout_reports_controller_error_and_closes_listener(self):
        timeout = asyncio.timeout
        with patch("joydance.games.abstract_game_wrapper.asyncio.timeout", side_effect=lambda seconds: timeout(0.02)) as deadline:
            with self.assertRaises(TimeoutError):
                await self.game.hole_punching()
        deadline.assert_called_once_with(10)
        self.assertEqual(self.listener.fileno(), -1)
        self.assertIsNone(self.game.console_conn)
        self.state_changed.assert_awaited_once_with(
            self.game.controller.serial, {"state": PairingState.ERROR_HOLE_PUNCHING.value},
        )

    async def test_stop_cancels_pending_accept_without_reporting_pairing_error(self):
        self.game.requires_punch_pairing = True
        states = []
        self.state_changed.side_effect = lambda serial, state: states.append(state["state"])
        with (
            patch.object(self.game, "get_access_token", new=AsyncMock()),
            patch.object(self.game, "send_pairing_code", new=AsyncMock()),
            patch.object(self.game, "send_initiate_punch_pairing", new=AsyncMock()),
            patch.object(self.game, "connect_ws", new=AsyncMock()) as connect,
        ):
            task = await self.game.start_pair()
            await asyncio.sleep(0)
            self.assertGreater(self.listener.getsockname()[1], 0)
            await asyncio.wait_for(self.game.disconnect(should_reconnect=False), 1)
            connect.assert_not_awaited()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.listener.fileno(), -1)
        self.assertIsNone(self.game.pair_task)
        self.assertIsNone(self.game.reconnection_task)
        self.assertFalse(self.game._state["connection_requested"])
        self.assertNotIn(PairingState.ERROR_HOLE_PUNCHING.value, states)

    async def test_bind_failure_preserves_original_error_and_closes_listener(self):
        self.listener.bind(("127.0.0.1", 0))
        with self.assertRaises(OSError):
            await self.game.hole_punching()
        self.assertEqual(self.listener.fileno(), -1)
        self.state_changed.assert_awaited_once_with(
            self.game.controller.serial, {"state": PairingState.ERROR_HOLE_PUNCHING.value},
        )

    @staticmethod
    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
