import asyncio
import json
import socket
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import serve

from joydance.games import discovery


class ConsoleDiscoveryParsingTests(unittest.TestCase):
    def test_legacy_relay_preserves_the_game_address_port_and_path(self):
        console = discovery._parse_console({
            "protocol": "v1.phonescoring.jd.ubisoft.com", "titleId": "JD2017-PC",
            "consoleName": "Living room", "pairingUrl": "http://192.168.1.8:9000/game/?token=123",
        }, "192.168.1.9")
        self.assertEqual(console, {
            "name": "Living room", "protocol": "v1", "console_ip_addr": "192.168.1.8",
            "console_url": "ws://192.168.1.8:9000/game/smartphone?token=123",
        })

    def test_legacy_reply_without_url_uses_sender(self):
        console = discovery._parse_console({
            "protocol": "v1.phonescoring.jd.ubisoft.com", "titleId": "JD2017-PC",
        }, "192.168.1.8")
        self.assertEqual(console["console_url"], "ws://192.168.1.8:8080/smartphone")
        self.assertEqual(console["name"], "JD2017-PC")

    def test_v2_relay_returns_only_a_code_not_a_direct_game_address(self):
        console = discovery._parse_console({
            "protocol": "v2.phonescoring.jd.ubisoft.com", "pairingCode": "012345",
            "pairingUrl": "ws://untrusted.example", "consoleName": ["invalid"],
        }, "192.168.1.9")
        self.assertEqual(console, {"name": "Just Dance", "protocol": "v2", "pairing_code": "012345"})

    def test_rejects_malformed_replies_and_nonlocal_legacy_urls(self):
        v1 = {"protocol": "v1.phonescoring.jd.ubisoft.com", "titleId": "JD2017-PC"}
        v2 = {"protocol": "v2.phonescoring.jd.ubisoft.com", "pairingCode": "123456"}
        for reply in (None, [], {}, {**v1, "titleId": []}, {**v1, "titleId": ""},
                      {**v2, "protocol": "unknown"}, *({**v2, "pairingCode": code} for code in (
                          123456, [], None, "12345", "123456\n", "１２３４５６",
                      ))):
            with self.subTest(reply=reply):
                self.assertIsNone(discovery._parse_console(reply, "192.168.1.8"))
        for url in (
            None, [], "ws://8.8.8.8:8080", "ws://example.com:8080", "file:///etc/passwd",
            "ws://192.168.1.8:0", "ws://192.168.1.8:65536", "ws://192.168.1.8:bad",
            "ws://user:pass@192.168.1.8:8080", "ws://192.168.1.8:8080/#fragment",
            "ws://[invalid", " ws://192.168.1.8:8080", "ws://192.168.1.8/\npath",
        ):
            with self.subTest(url=url):
                self.assertIsNone(discovery._parse_console({**v1, "pairingUrl": url}, "192.168.1.8"))
        self.assertIsNone(discovery._parse_console(v2, "8.8.8.8"))


class ConsoleDiscoveryIdentityTests(unittest.IsolatedAsyncioTestCase):
    def reply(self, address, **changes):
        return ({
            "protocol": "v1.phonescoring.jd.ubisoft.com", "titleId": "PC-JD2017",
            "consoleName": "ROADAR-PC", **changes,
        }, address)

    async def discover(self, packets, host="192.168.1.8", lookup_error=False):
        async def replies(host_ip):
            for packet in packets:
                yield packet

        with (
            patch.object(discovery, "_discovery_replies", side_effect=replies),
            patch.object(discovery.socket, "gethostbyname_ex",
                         return_value=("this-pc", [], ["192.168.1.8", "172.19.0.1"]),
                         side_effect=OSError("No address lookup") if lookup_error else None),
        ):
            return await discovery.discover_consoles(host)

    async def test_local_interface_aliases_merge_and_prefer_lan_in_any_reply_order(self):
        packets = [self.reply(address) for address in ("127.0.0.1", "172.19.0.1", "192.168.1.8")]
        for order in (packets, list(reversed(packets))):
            with self.subTest(order=order):
                consoles = await self.discover(order)
                self.assertEqual(len(consoles), 1)
                self.assertEqual(consoles[0]["console_ip_addr"], "192.168.1.8")
                self.assertEqual(set(consoles[0]["console_urls"]), {
                    "ws://127.0.0.1:8080/smartphone", "ws://172.19.0.1:8080/smartphone",
                    "ws://192.168.1.8:8080/smartphone",
                })

    async def test_non_loopback_address_is_preferred_when_configured_host_is_absent(self):
        consoles = await self.discover([self.reply("127.0.0.1"), self.reply("172.19.0.1")])
        self.assertEqual(consoles[0]["console_ip_addr"], "172.19.0.1")

    async def test_remote_games_with_matching_names_and_titles_stay_separate(self):
        consoles = await self.discover([self.reply("192.168.1.40"), self.reply("192.168.1.41")])
        self.assertEqual(len(consoles), 2)

    async def test_different_titles_ports_paths_and_queries_stay_separate(self):
        consoles = await self.discover([
            self.reply("127.0.0.1"), self.reply("192.168.1.8"),
            self.reply("192.168.1.8", titleId="PC-JD2018"),
            self.reply("192.168.1.8", pairingUrl="http://192.168.1.8:9000/"),
            self.reply("192.168.1.8", pairingUrl="http://192.168.1.8:8080/other/"),
            self.reply("192.168.1.8", pairingUrl="http://192.168.1.8:8080/?slot=2"),
        ])
        self.assertEqual(len(consoles), 5)

    async def test_relay_sender_does_not_determine_game_identity(self):
        consoles = await self.discover([
            self.reply("127.0.0.1", pairingUrl="http://192.168.1.40:8080/"),
            self.reply("127.0.0.1", pairingUrl="http://192.168.1.41:8080/"),
        ])
        self.assertEqual(len(consoles), 2)

    async def test_address_lookup_failure_does_not_guess_which_remote_ips_are_local(self):
        consoles = await self.discover([self.reply("127.0.0.1"), self.reply("192.168.1.8")], lookup_error=True)
        self.assertEqual(len(consoles), 2)


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
            kwargs.update(open_timeout=0.25, close_timeout=0.05)
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

    async def test_scan_collects_both_protocols_and_deduplicates_relay_replies(self):
        task = asyncio.create_task(discovery.discover_consoles("127.0.0.1"))
        self.addAsyncCleanup(self.cancel, task)
        await self.wait_for_probe()
        v1 = {
            "protocol": "v1.phonescoring.jd.ubisoft.com", "titleId": "JD2017-PC",
            "consoleName": "My PC", "pairingUrl": "http://192.168.1.8:9000/",
        }
        for reply in (b"not json", b"\xff", b"[" * 1500 + b"]" * 1500, [], {}, self.reply(), v1, self.reply(), v1):
            await self.send(reply)
        consoles = await asyncio.wait_for(task, 1)
        self.assertEqual(len(consoles), 2)
        self.assertEqual(consoles[0]["pairing_code"], "123456")
        self.assertEqual(consoles[1]["console_url"], "ws://192.168.1.8:9000/smartphone")
        self.assertEqual(self.game_messages, [])

    async def test_empty_scan_and_busy_port_are_distinguishable(self):
        self.assertEqual(await discovery.discover_consoles("127.0.0.1"), [])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
            busy.bind(("0.0.0.0", self.phone_port))
            with self.assertRaises(OSError):
                await discovery.discover_consoles("127.0.0.1")

    async def test_unreachable_probe_target_does_not_abort_the_scan(self):
        loop = asyncio.get_running_loop()
        receive = loop.sock_recvfrom
        reset_received = False

        async def receive_with_reset(sock, size):
            nonlocal reset_received
            if sock.getsockname()[1] == self.phone_port and not reset_received:
                reset_received = True
                raise ConnectionResetError(10054, "ICMP port unreachable")
            return await receive(sock, size)

        with patch.object(loop, "sock_recvfrom", side_effect=receive_with_reset):
            task = asyncio.create_task(discovery.discover_consoles("127.0.0.1"))
            self.addAsyncCleanup(self.cancel, task)
            await self.wait_for_probe()
            await self.send(self.reply())
            consoles = await asyncio.wait_for(task, 1)
        self.assertTrue(reset_received)
        self.assertEqual(consoles[0]["pairing_code"], "123456")

    async def test_scan_and_pairing_share_the_port_without_losing_replies(self):
        scan = asyncio.create_task(discovery.discover_consoles("127.0.0.1"))
        self.addAsyncCleanup(self.cancel, scan)
        await self.wait_for_probe()
        pair = asyncio.create_task(discovery.discover_jdmo_game("123456", "127.0.0.1"))
        self.addAsyncCleanup(self.cancel, pair)

        async def respond():
            while True:
                await self.send(self.reply())
                await self.wait_for_probe()

        responder = asyncio.create_task(respond())
        self.addAsyncCleanup(self.cancel, responder)
        self.assertEqual(len(await asyncio.wait_for(scan, 1)), 1)
        self.assertEqual(await asyncio.wait_for(pair, 1), "127.0.0.1")

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
