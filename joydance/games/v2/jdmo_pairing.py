"""Pairing service and local endpoint discovery for JDMO 2021."""

import json
import logging
from contextlib import aclosing

import aiohttp
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from joydance.constants import JDMO_PAIRING_URL, JD_SERVICE_TIMEOUT, PairingState, WS_SUBPROTOCOLS, WsSubprotocolVersion
from joydance.utils import GameConnectionError
from ..discovery import GAME_PORT, discovery_replies


logger = logging.getLogger("joydance")


async def request_pairing(path, *, headers, pairing_code, payload=None):
    is_punch = payload is not None
    state = PairingState.ERROR_PUNCH_PAIRING if is_punch else PairingState.ERROR_CONNECTION
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
            for version in (1, 2):
                url = f"{JDMO_PAIRING_URL}/v{version}/{path}"
                async with session.request(
                    "POST" if is_punch else "GET", url,
                    params=None if is_punch else {"code": pairing_code}, json=payload,
                ) as resp:
                    body = await resp.text()
                    try:
                        data = json.loads(body)
                    except ValueError:
                        data = body
                    # The JDMO app retries v2 only when v1 has no route, not for invalid-code JSON errors.
                    if version == 1 and resp.status in {404, 405} and (data is None or isinstance(data, str)):
                        continue
                    if not 200 <= resp.status < 300:
                        invalid_code = not is_punch and resp.status in {400, 404, 410}
                        raise GameConnectionError(
                            "Invalid or expired pairing code." if invalid_code else f"JDMO pairing service rejected the request (HTTP {resp.status}).",
                            PairingState.ERROR_INVALID_PAIRING_CODE if invalid_code else state,
                            should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                        )
                    return data
    except (aiohttp.ClientError, TimeoutError) as error:
        raise GameConnectionError(f"JDMO pairing request failed: {str(error) or 'request timed out'}.", state) from error


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
        async with aclosing(discovery_replies(host_ip)) as replies:
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
