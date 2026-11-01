import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dance
from joydance.config_handler import ConfigHandler


class SavedConnectionsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "config.cfg")
        with patch("joydance.config_handler.get_host_ip", return_value="192.168.1.2"):
            self.config = ConfigHandler(self.path)
        self.app = web.Application()
        self.app.update({"config_handler": self.config, "websockets": set()})
        self.app.router.add_get("/", dance.html_handler)
        self.app.router.add_get("/connections", dance.connections_handler)
        self.app.router.add_post("/connections", dance.connections_handler)
        self.app.router.add_delete("/connections/{id}", dance.connections_handler)
        self.app.router.add_get("/ws", dance.websocket_handler)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.entry = {"name": "Living room", "pairing_method": "old", "console_ip_addr": "192.168.1.3"}

    async def test_changes_are_persisted_and_sent_to_all_browsers(self):
        browsers = [await self.client.ws_connect("/ws"), await self.client.ws_connect("/ws")]
        await browsers[0].send_json({"cmd": "get_connections"})
        self.assertEqual(await browsers[0].receive_json(timeout=1), {"cmd":"resp_get_connections", "data":[]})
        response = await self.client.post("/connections", json=self.entry)
        self.assertEqual(response.status, 200)
        data = await response.json()
        entry = data["connection"]
        self.assertEqual(ConfigHandler(self.path).connections, [entry])
        for browser in browsers:
            self.assertEqual(await browser.receive_json(timeout=1), {"cmd":"resp_get_connections", "data":[entry]})
        response = await self.client.post("/connections", json={**entry, "name":"Updated"})
        updated = (await response.json())["connection"]
        for browser in browsers:
            self.assertEqual((await browser.receive_json(timeout=1))["data"], [updated])
        response = await self.client.delete("/connections/" + entry["id"])
        self.assertEqual(await response.json(), {"connections":[]})
        for browser in browsers:
            self.assertEqual((await browser.receive_json(timeout=1))["data"], [])
        self.assertEqual(ConfigHandler(self.path).connections, [])

    async def test_invalid_requests_and_save_errors_are_reported(self):
        response = await self.client.post("/connections", data=json.dumps(self.entry), headers={"Content-Type":"text/plain"})
        self.assertEqual(response.status, 400)
        for payload in (None, [], "bad", {}, {**self.entry, "name":""}, {**self.entry, "console_ip_addr":""}):
            with self.subTest(payload=payload):
                response = await self.client.post("/connections", json=payload)
                self.assertEqual(response.status, 400)
                self.assertIn("error", await response.json())
        response = await self.client.delete("/connections/missing")
        self.assertEqual(response.status, 404)
        with patch.object(self.config, "save_data", return_value=False):
            response = await self.client.post("/connections", json=self.entry)
        self.assertEqual(response.status, 503)
        self.assertEqual(self.config.connections, [])

    async def test_connection_names_cannot_escape_the_bootstrap_script(self):
        entry = {**self.entry, "name": "</script><script>alert('name')</script>"}
        response = await self.client.post("/connections", json=entry)
        self.assertEqual(response.status, 200)
        response = await self.client.get("/")
        source = await response.text()
        self.assertNotIn(entry["name"], source)
        match = re.search(r"window.CONNECTIONS = (.*?); window.VERSION", source)
        self.assertEqual(json.loads(match.group(1))[0]["name"], entry["name"])
