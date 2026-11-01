import asyncio
import json
import logging
import re
import socket
from contextlib import aclosing
from ipaddress import IPv4Address
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from joydance.constants import WS_SUBPROTOCOLS, WsSubprotocolVersion
from joydance.utils.network import is_local_ipv4, normalize_console_url


logger = logging.getLogger("joydance")
CONSOLE_PORT = 6000
PHONE_PORT = 7000
GAME_PORT = 8080
DISCOVERY_TIMEOUT = 5
PROBE_INTERVAL = 0.5
_discovery_lock = asyncio.Lock()


def _parse_console(info, address):
    if not isinstance(info, dict) or not is_local_ipv4(address):
        return None
    if info.get("protocol") == WS_SUBPROTOCOLS[WsSubprotocolVersion.V2.value]:
        code = info.get("pairingCode")
        if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code):
            # V2 relays advertise a code, not their own address as the game endpoint.
            return {"name": "Just Dance", "protocol": "v2", "pairing_code": code}
    elif info.get("protocol") == WS_SUBPROTOCOLS[WsSubprotocolVersion.V1.value]:
        title = info.get("titleId")
        if not isinstance(title, str) or not title.strip():
            return None
        try:
            url = normalize_console_url(info.get("pairingUrl", f"http://{address}:{GAME_PORT}"))
        except ValueError:
            return None
        name = info.get("consoleName")
        return {
            "name": name[:100] if isinstance(name, str) and name.strip() else title[:100],
            "protocol": "v1", "console_ip_addr": urlparse(url).hostname, "console_url": url,
        }
    return None


async def _discovery_replies(host_ip):
    # Replies always target port 7000; serialize scans and controller pairing probes.
    async with _discovery_lock:
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe.bind(("0.0.0.0", PHONE_PORT))
            probe.setblocking(False)
            targets = {"127.0.0.1", "255.255.255.255"}
            if is_local_ipv4(host_ip):
                targets.add(host_ip)
            scan_deadline = loop.time() + DISCOVERY_TIMEOUT
            while loop.time() < scan_deadline:
                for target in targets:
                    try:
                        await loop.sock_sendto(probe, b"phonescoring.jd.ubisoft.com", (target, CONSOLE_PORT))
                    except OSError:
                        continue
                deadline = min(loop.time() + PROBE_INTERVAL, scan_deadline)
                while loop.time() < deadline:
                    try:
                        async with asyncio.timeout_at(deadline):
                            packet, peer = await loop.sock_recvfrom(probe, 4096)
                    except TimeoutError:
                        break
                    except ConnectionResetError:
                        # Windows reports an ICMP port-unreachable reply as a UDP reset.
                        continue
                    try:
                        info = json.loads(packet)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(info, dict) and is_local_ipv4(peer[0]):
                        yield info, peer[0]


async def discover_consoles(host_ip):
    """Collect local games without connecting a player or resolving codes online."""
    try:
        _, _, local_addresses = await asyncio.to_thread(socket.gethostbyname_ex, socket.gethostname())
    except OSError:
        local_addresses = []
    consoles = {}
    async with aclosing(_discovery_replies(host_ip)) as replies:
        async for info, address in replies:
            console = _parse_console(info, address)
            if not console:
                continue
            if console["protocol"] == "v2":
                if len(consoles) < 64:
                    consoles.setdefault(("v2", console["pairing_code"]), console)
                continue
            console["title_id"] = info["titleId"]
            uri = urlparse(console["console_url"])
            is_loopback = IPv4Address(uri.hostname).is_loopback
            # Only merge interface aliases we can identify as this machine's addresses.
            host = "this-host" if is_loopback or uri.hostname in local_addresses else uri.hostname
            key = ("v1", info["titleId"], host, uri.port or 80, uri.path, uri.query)
            previous = consoles.get(key)
            if previous is None:
                if len(consoles) < 64:
                    console["console_urls"] = [console["console_url"]]
                    consoles[key] = console
                continue
            if console["console_url"] not in previous["console_urls"] and len(previous["console_urls"]) < 64:
                previous["console_urls"].append(console["console_url"])
            previous_uri = urlparse(previous["console_url"])
            if (uri.hostname == host_ip, not is_loopback) > (
                previous_uri.hostname == host_ip, not IPv4Address(previous_uri.hostname).is_loopback,
            ):
                previous.update(console)
    return list(consoles.values())


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
    checked = set()
    try:
        async with aclosing(_discovery_replies(host_ip)) as replies:
            async for info, address in replies:
                if (info.get("pairingCode") != pairing_code
                        or info.get("protocol") != WS_SUBPROTOCOLS[WsSubprotocolVersion.V2.value]
                        or address in checked):
                    continue
                checked.add(address)
                if await _is_game_endpoint(address):
                    return address
    except (OSError, TimeoutError) as error:
        logger.debug("Local JDMO discovery unavailable: %s", error)
    return None
