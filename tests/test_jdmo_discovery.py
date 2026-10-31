import asyncio
import json
import socket
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import serve

from joydance.games import discovery


class JdmoDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.setblocking(False)
        self.addCleanup(self.server.close)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as phone:
            phone.bind(("127.0.0.1", 0))
            self.phone_port = phone.getsockname()[1]
        for name, value in (
            ("CONSOLE_PORT", self.server.getsockname()[1]), ("PHONE_PORT", self.phone_port),
            ("DISCOVERY_TIMEOUT", 0.5), ("PROBE_INTERVAL", 0.02), ("_discovery_lock", asyncio.Lock()),
        ):
            replacement = patch.object(discovery, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def asyncSetUp(self):
        self.game_messages = []

        async def game(websocket):
            async for message in websocket:
                self.game_messages.append(message)

        self.game_endpoint = await self.enterAsyncContext(serve(
            game, "127.0.0.1", 0, subprotocols=["v2.phonescoring.jd.ubisoft.com"],
        ))
        replacement = patch.object(discovery, "GAME_PORT", self.game_endpoint.sockets[0].getsockname()[1])
        replacement.start()
        self.addCleanup(replacement.stop)

        def connect_fast(uri, **kwargs):
            kwargs.update(open_timeout=0.1, close_timeout=0.02)
            return websocket_connect(uri, **kwargs)

        replacement = patch.object(discovery, "connect", side_effect=connect_fast)
        replacement.start()
        self.addCleanup(replacement.stop)

    async def send(self, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        await asyncio.get_running_loop().sock_sendto(self.server, data, ("127.0.0.1", self.phone_port))

    async def wait_for_probe(self):
        packet, _ = await asyncio.wait_for(asyncio.get_running_loop().sock_recvfrom(self.server, 4096), 1)
        self.assertEqual(packet, b"phonescoring.jd.ubisoft.com")

    def reply(self, code="123456", protocol="v2.phonescoring.jd.ubisoft.com"):
        return {"pairingCode": code, "protocol": protocol, "pairingUrl": "ws://untrusted.example/"}

    async def cancel(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_only_matching_v2_replies_select_the_actual_sender(self):
        task = asyncio.create_task(discovery.discover_jdmo_game("123456", "127.0.0.1"))
        self.addAsyncCleanup(self.cancel, task)
        await self.wait_for_probe()
        for invalid in (b"not json", [], self.reply(code="654321"), self.reply(protocol="v1.phonescoring.jd.ubisoft.com")):
            await self.send(invalid)
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        await self.send(self.reply())
        self.assertEqual(await asyncio.wait_for(task, 1), "127.0.0.1")
        self.assertEqual(self.game_messages, [])

    async def test_discovery_timeout_returns_no_match(self):
        self.assertIsNone(await discovery.discover_jdmo_game("123456", "127.0.0.1"))

    async def test_phone_relay_is_skipped_before_selecting_the_game(self):
        task = asyncio.create_task(discovery.discover_jdmo_game("123456", "127.0.0.1"))
        self.addAsyncCleanup(self.cancel, task)
        await self.wait_for_probe()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as relay:
            relay.bind(("127.0.0.2", 0))
            relay.setblocking(False)
            await asyncio.get_running_loop().sock_sendto(
                relay, json.dumps(self.reply()).encode(), ("127.0.0.1", self.phone_port),
            )
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            await self.send(self.reply())
            self.assertEqual(await asyncio.wait_for(task, 1), "127.0.0.1")

    async def test_unrelated_websocket_is_rejected_and_checked_only_once(self):
        connections = []

        async def unrelated(websocket):
            connections.append(websocket)
            await websocket.wait_closed()

        async with serve(unrelated, "127.0.0.2", discovery.GAME_PORT):
            task = asyncio.create_task(discovery.discover_jdmo_game("123456", "127.0.0.1"))
            self.addAsyncCleanup(self.cancel, task)
            await self.wait_for_probe()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as relay:
                relay.bind(("127.0.0.2", 0))
                relay.setblocking(False)
                for _ in range(3):
                    await asyncio.get_running_loop().sock_sendto(
                        relay, json.dumps(self.reply()).encode(), ("127.0.0.1", self.phone_port),
                    )
                await self.send(self.reply())
                self.assertEqual(await asyncio.wait_for(task, 1), "127.0.0.1")
            self.assertEqual(len(connections), 1)

    async def test_stalled_relay_does_not_hide_a_later_game_reply(self):
        probing = asyncio.Event()

        async def stalled(reader, writer):
            try:
                await reader.read(4096)
                probing.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(stalled, "127.0.0.2", discovery.GAME_PORT):
            task = asyncio.create_task(discovery.discover_jdmo_game("123456", "127.0.0.1"))
            self.addAsyncCleanup(self.cancel, task)
            await self.wait_for_probe()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as relay:
                relay.bind(("127.0.0.2", 0))
                relay.setblocking(False)
                await asyncio.get_running_loop().sock_sendto(
                    relay, json.dumps(self.reply()).encode(), ("127.0.0.1", self.phone_port),
                )
                await asyncio.wait_for(probing.wait(), 1)
                await self.send(self.reply())
                self.assertEqual(await asyncio.wait_for(task, 1), "127.0.0.1")

    async def test_busy_phone_port_returns_no_match(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
            busy.bind(("0.0.0.0", self.phone_port))
            self.assertIsNone(await discovery.discover_jdmo_game("123456", "127.0.0.1"))

    async def test_cancellation_releases_the_phone_port(self):
        task = asyncio.create_task(discovery.discover_jdmo_game("123456", "127.0.0.1"))
        self.addAsyncCleanup(self.cancel, task)
        await self.wait_for_probe()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as next_probe:
            next_probe.bind(("0.0.0.0", self.phone_port))

    async def test_two_controllers_can_discover_without_competing_for_the_port(self):
        async def respond():
            for _ in range(2):
                await self.wait_for_probe()
                await self.send(self.reply())

        responder = asyncio.create_task(respond())
        self.addAsyncCleanup(self.cancel, responder)
        results = await asyncio.wait_for(asyncio.gather(
            discovery.discover_jdmo_game("123456", "127.0.0.1"),
            discovery.discover_jdmo_game("123456", "127.0.0.1"),
        ), 1)
        self.assertEqual(results, ["127.0.0.1", "127.0.0.1"])
        await responder
