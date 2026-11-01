import asyncio
from collections import defaultdict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dance
from joydance.config_handler import ConfigHandler
from joydance.games.justdance_v1 import JustDanceGameV1


class ConsoleDiscoveryBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = {
            "pairing_method": "default", "host_ip_addr": "192.168.1.2",
            "console_ip_addr": "192.168.1.3", "pairing_code": "123456",
        }
        self.app = web.Application()
        self.app.update({
            "websockets": set(), "joydance_connections": {},
            "config_handler": SimpleNamespace(data=self.config.copy(), validate_config_with_error=ConfigHandler.validate_config_with_error),
        })
        self.app.router.add_get("/discovery", dance.discovery_handler)
        self.app.router.add_get("/ws", dance.websocket_handler)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_scan_is_opt_in_and_does_not_change_configuration_or_connect(self):
        consoles = [{"protocol": "v2", "name": "Just Dance", "pairing_code": "012345"}]
        with patch.object(dance, "discover_consoles", new=AsyncMock(return_value=consoles)) as scan:
            browser = await self.client.ws_connect("/ws")
            scan.assert_not_awaited()
            response = await self.client.get("/discovery")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"consoles": consoles})
            scan.assert_awaited_once_with("192.168.1.2")
            self.assertEqual(self.app["config_handler"].data, self.config)
            self.assertEqual(self.app["joydance_connections"], {})
            await browser.close()

    async def test_scan_failure_returns_an_actionable_error(self):
        with patch.object(dance, "discover_consoles", new=AsyncMock(side_effect=OSError("Port busy"))):
            response = await self.client.get("/discovery")
            self.assertEqual(response.status, 503)
            self.assertIn("7000", (await response.json())["error"])

    async def test_scanning_does_not_delay_browser_disconnect_commands(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def scan(host):
            started.set()
            await finish.wait()
            return []

        with (patch.object(dance, "discover_consoles", side_effect=scan),
              patch.object(dance, "disconnect_controller", new=AsyncMock()) as disconnect):
            browser = await self.client.ws_connect("/ws")
            task = asyncio.ensure_future(self.client.get("/discovery"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                await browser.send_json({"cmd": "disconnect_controller", "data": {"controller_serial": "test"}})
                self.assertEqual((await browser.receive_json(timeout=1))["cmd"], "resp_disconnect_controller")
                disconnect.assert_awaited_once()
                self.assertFalse(task.done())
            finally:
                finish.set()
                await task

    async def test_discovered_url_is_validated_and_passed_to_the_legacy_connection(self):
        controller = SimpleNamespace(serial="test", set_accel_collection=Mock(), set_player_led=AsyncMock())
        self.app.update({
            "controllers": {"test": controller}, "controllers_info": defaultdict(dict),
            "joydance_connection_locks": defaultdict(asyncio.Lock),
        })
        data = {**self.config, "controller_serial": "test", "pairing_method": "old",
                "console_url": "http://192.168.1.3:9000/custom/"}
        with patch.object(JustDanceGameV1, "start_pair", new=AsyncMock(return_value=Mock())):
            await dance.connect_controller(self.app, None, data)
        game = self.app["joydance_connections"]["test"]
        self.assertEqual(game.console_url, "ws://192.168.1.3:9000/custom/smartphone")
        self.assertNotIn("console_url", self.app["config_handler"].data)
        for changes in (
            {"console_url": "http://8.8.8.8:9000/"},
            {"console_url": "http://192.168.1.4:9000/"},
            {"console_ip_addr": "", "console_url": None},
            {"pairing_method": "jdmo"},
        ):
            with self.subTest(changes=changes), self.assertLogs("dance", level="ERROR"):
                with self.assertRaises(ValueError):
                    await dance.connect_controller(self.app, None, {**data, **changes})
        self.assertIs(self.app["joydance_connections"]["test"], game)
