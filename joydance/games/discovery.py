import asyncio
import json
import logging
import socket
from ipaddress import IPv4Address

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from joydance.constants import WS_SUBPROTOCOLS, WsSubprotocolVersion
from joydance.utils.network import is_private_ipv4


logger = logging.getLogger("joydance")
CONSOLE_PORT = 6000
PHONE_PORT = 7000
GAME_PORT = 8080
DISCOVERY_TIMEOUT = 5
PROBE_INTERVAL = 0.5
_discovery_lock = asyncio.Lock()


async def _is_game_endpoint(address):
    """Check the V2 WebSocket upgrade without sending Hello or joining a player."""
    protocol = WS_SUBPROTOCOLS[WsSubprotocolVersion.V2.value]
    try:
        async with connect(
            f"ws://{address}:{GAME_PORT}/smartphone", subprotocols=[protocol],
            open_timeout=1, close_timeout=0.1, ping_interval=None, proxy=None,
        ) as websocket:
            return websocket.subprotocol == protocol
    except (OSError, TimeoutError, WebSocketException):
        return False


async def discover_jdmo_game(pairing_code, host_ip):
    # Replies always target port 7000; serialize probes from different controllers.
    async with _discovery_lock:
        try:
            loop = asyncio.get_running_loop()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                probe.bind(("0.0.0.0", PHONE_PORT))
                probe.setblocking(False)
                targets = {"127.0.0.1", "255.255.255.255", host_ip} - {None, ""}
                checked = set()
                async with asyncio.timeout(DISCOVERY_TIMEOUT):
                    while True:
                        for target in targets:
                            try:
                                await loop.sock_sendto(probe, b"phonescoring.jd.ubisoft.com", (target, CONSOLE_PORT))
                            except OSError:
                                continue
                        deadline = loop.time() + PROBE_INTERVAL
                        while loop.time() < deadline:
                            try:
                                async with asyncio.timeout_at(deadline):
                                    packet, peer = await loop.sock_recvfrom(probe, 4096)
                            except TimeoutError:
                                break
                            try:
                                info = json.loads(packet)
                            except ValueError:
                                continue
                            if not isinstance(info, dict) or info.get("pairingCode") != pairing_code:
                                continue
                            if info.get("protocol") != WS_SUBPROTOCOLS[WsSubprotocolVersion.V2.value]:
                                continue
                            address = peer[0]
                            if address in checked or not (is_private_ipv4(address) or IPv4Address(address).is_loopback):
                                continue
                            checked.add(address)
                            if await _is_game_endpoint(address):
                                return address
        except (OSError, TimeoutError) as error:
            logger.debug("Local JDMO discovery unavailable: %s", error)
    return None
