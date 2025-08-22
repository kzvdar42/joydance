import logging
from urllib.parse import urlparse
import ssl
import websockets

import asyncio

from joydance.constants import (
    Command,
    PairingState,
    WS_SUBPROTOCOLS,
    WsSubprotocolVersion,
)
from .justdance_abstract import JustDanceGameAbstract


logger = logging.getLogger("joydance")


class JustDanceGameV2(JustDanceGameAbstract):
    """
    Game protocol handler for Just Dance protocol version v2.
    Handles all v2-specific communication, message parsing, and command preprocessing.
    """

    def __init__(self, controller, *args, **kwargs):
        super().__init__(controller, *args, **kwargs)

    async def connect_ws(self):
        server_hostname = None
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.set_ciphers("ALL")
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        if self.tls_certificate:
            try:
                ssl_context.load_verify_locations(cadata=self.tls_certificate)
            except Exception as e:
                logger.error(f"{self.controller.serial}: V2 Failed to load TLS certificate: {e}")

        if self.pairing_url.startswith("wss://192.168.") or self.pairing_url.startswith("wss://10."):
            if self.console_conn:
                server_hostname = self.console_conn.getpeername()[0]

        if not server_hostname:
            try:
                tmp = urlparse(self.pairing_url)
                server_hostname = tmp.hostname
            except Exception as e:
                logger.error(
                    f"{self.controller.serial}: V2 Error parsing server_hostname from {self.pairing_url}: {e}"
                )

        subprotocol = WS_SUBPROTOCOLS[WsSubprotocolVersion.V2.value]
        try:
            logger.debug(
                f"{self.controller.serial}: V2 connecting to {self.pairing_url} with subprotocol {subprotocol}, server_hostname: {server_hostname}"
            )
            async with websockets.connect(
                self.pairing_url,
                subprotocols=[subprotocol],
                sock=self.console_conn,
                ssl=ssl_context,
                ping_timeout=None,
                server_hostname=server_hostname,
            ) as websocket:
                self.ws = websocket
                self.is_connected = True
                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.CONNECTED.value}
                )

                receive_task = asyncio.create_task(self._message_receive_loop())

                await asyncio.gather(self.send_hello(), self.tick(), self.send_command())

        except websockets.ConnectionClosed as e:
            logger.warning(f"{self.controller.serial}: V2 WebSocket connection closed: {e}")
            await self.on_state_changed(
                self.controller.serial,
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect()
        except ConnectionRefusedError as e:
            logger.error(f"{self.controller.serial}: V2 Connection refused for {self.pairing_url}: {e}")
            await self.on_state_changed(
                self.controller.serial,
                {"state": PairingState.ERROR_CONNECTION.value, "error_details": str(e)},
            )
            await self.disconnect()
        except Exception as e:
            logger.exception(
                "%s: V2 An error occurred while connecting WebSocket to console.",
                self.controller.serial,
            )
            await self.on_state_changed(
                self.controller.serial,
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect()
        finally:
            if "receive_task" in locals() and not receive_task.done():
                receive_task.cancel()

    async def _message_receive_loop(self):
        try:
            async for message_str in self.ws:
                await self.on_message(message_str)
        except websockets.ConnectionClosed:
            logger.info(
                "%s: V2 WebSocket connection closed while receiving messages.",
                self.controller.serial,
            )
        except Exception as e:
            logger.exception("%s: V2 Error in message receive loop.", self.controller.serial)
            await self.disconnect()

    async def pair(self):
        try:
            self.is_connected = False
            self.should_reconnect = True

            if self.console_ip_addr:
                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.CONNECTING.value}
                )
                self.pairing_url = f"wss://{self.console_ip_addr}:8080/smartphone"
                if getattr(self, "requires_punch_pairing", False):
                    logger.debug(
                        "%s: V2 Direct IP specified, punch pairing required by config/flag.",
                        self.controller.serial,
                    )
                    if not self.host_ip_addr or not self.host_port:
                        logger.error(
                            "%s: V2 Punch pairing required for direct IP but host_ip_addr/host_port not set.",
                            self.controller.serial,
                        )
                        raise Exception(
                            "Host IP/Port not set for required punch pairing with direct IP."
                        )
                    await self.hole_punching()
            else:
                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.GETTING_TOKEN.value}
                )
                logger.debug("%s: V2 Getting authorization token...", self.controller.serial)
                await self.get_access_token()

                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.PAIRING.value}
                )
                logger.debug("%s: V2 Sending pairing code...", self.controller.serial)
                await self.send_pairing_code()

                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.CONNECTING.value}
                )
                logger.debug("%s: V2 Connecting with console...", self.controller.serial)
                if self.requires_punch_pairing:
                    await self.send_initiate_punch_pairing()
                    await self.hole_punching()

            await self.connect_ws()
        except Exception:
            await self.disconnect()
            logger.exception("%s: V2 An error occurred while pairing.", self.controller.serial)

    async def preprocess_command(self, cmd):
        data = {}
        if cmd == Command.PAUSE:
            __class = "JD_Pause_PhoneCommandData"
        elif isinstance(cmd.value, str):
            __class = "JD_Custom_PhoneCommandData"
            data["identifier"] = cmd.value
        else:
            __class = "JD_Input_PhoneCommandData"
            data["input"] = cmd.value
        return __class, data
