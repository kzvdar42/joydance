import logging
import json
import time
from urllib.parse import urlparse
import ssl
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from joydance.constants import (
    Command,
    PairingState,
    WS_SUBPROTOCOLS,
    WsSubprotocolVersion,
    V2_INPUT_MAPPING_KEYS,
    V2_TAKEOVER_HOLD_DURATION,
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
        # Keep legacy commands until the server supplies its own mapping table.
        self._input_commands = None
        self._takeover_hold_enabled = False
        self._confirm_hold_started_at = None

    def _reset_input_state(self):
        super()._reset_input_state()
        self._input_commands = None
        self._takeover_hold_enabled = False
        self._confirm_hold_started_at = None

    def is_command_allowed(self, cmd):
        if not super().is_command_allowed(cmd):
            return False
        if self._takeover_hold_enabled and cmd == Command.ACCEPT and self._input_commands is None:
            return False
        if self._input_commands is not None and cmd in V2_INPUT_MAPPING_KEYS:
            return cmd in self._input_commands
        return True

    async def on_message(self, message_str):
        message = json.loads(message_str)
        message_class = message.get("__class")
        if message_class == "JD_OpenTakeMasterScreen_ConsoleCommandData":
            # A button already held when the prompt opens must not take over navigation.
            self.controller.get_latest_command(commands_to_check=set())
            self._takeover_hold_enabled = True
            self._confirm_hold_started_at = None
        elif message_class == "JD_ClosePopup_ConsoleCommandData":
            self._takeover_hold_enabled = False
            self._confirm_hold_started_at = None
        elif message_class in {
            "InputSetup_ConsoleCommandData", "EnableCarousel_ConsoleCommandData",
            "JD_PhoneUiSetupData", "JD_EnableAccelValuesSending_ConsoleCommandData",
        }:
            self._confirm_hold_started_at = None
        if message_class == "JD_InputCollectionPhoneData":
            self._parse_input_collection(message)
        await super().on_message(message_str)

    def _get_button_command(self, commands):
        cmd = super()._get_button_command(commands)
        if not self._takeover_hold_enabled:
            return cmd
        if Command.ACCEPT not in commands or not self.controller.is_command_pressed(Command.ACCEPT):
            self._confirm_hold_started_at = None
        elif cmd == Command.ACCEPT:
            self._confirm_hold_started_at = time.monotonic()
        elif (
            cmd is None and self._confirm_hold_started_at is not None
            and time.monotonic() - self._confirm_hold_started_at >= V2_TAKEOVER_HOLD_DURATION
        ):
            # Require another press before this hold can trigger again.
            self._confirm_hold_started_at = None
            return Command.ACCEPT
        return None if cmd == Command.ACCEPT else cmd

    def _parse_input_collection(self, message):
        # Discard queued presses so they cannot trigger newly assigned commands.
        self.controller.get_latest_command(commands_to_check=set())
        self._confirm_hold_started_at = None
        # The app replaces this connection-wide table independently of screen setup.
        self._input_commands = {}
        available_input = message.get("availableInput", {})
        if not isinstance(available_input, dict):
            logger.warning("%s: Invalid availableInput mapping table", self.controller.serial)
            available_input = {}
        for cmd, key in V2_INPUT_MAPPING_KEYS.items():
            encoded = available_input.get(key)
            if encoded is None or encoded == "":
                continue
            try:
                command = json.loads(encoded)["root"]
                if (
                    not isinstance(command, dict)
                    or not isinstance(command.get("__class"), str)
                    or not command["__class"]
                ):
                    raise ValueError("Expected a command object with a class name")
            except (KeyError, TypeError, ValueError):
                logger.warning("%s: Invalid input mapping for %s", self.controller.serial, key)
                continue
            self._input_commands[cmd] = command

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
                logger.error("%s: V2 Failed to load TLS certificate: %s", self.controller.serial, e)

        # TODO: handle other private IPs
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
            async with connect(
                self.pairing_url,
                subprotocols=[subprotocol],
                sock=self.console_conn,
                ssl=ssl_context,
                ping_timeout=None,
                max_size=None,  # Disable message size limit to handle large JD_PhoneUiSetupData messages
                server_hostname=server_hostname,
                proxy=None,
            ) as websocket:
                self.ws = websocket
                # Set state only on JD_PhoneDataCmdHandshakeContinue response
                await self.run_connection_tasks()

        except ConnectionClosed as e:
            logger.warning("%s: V2 WebSocket connection closed: %s", self.controller.serial, e)
            await self.on_state_changed(
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect()
        except ConnectionRefusedError as e:
            logger.error("%s: V2 Connection refused for %s: %s", self.controller.serial, self.pairing_url, e)
            await self.on_state_changed(
                {"state": PairingState.ERROR_CONSOLE_CONNECTION.value, "error_details": str(e)},
            )
            await self.disconnect()
        except Exception as e:
            logger.exception(
                "%s: V2 An error occurred while connecting WebSocket to console.",
                self.controller.serial,
            )
            await self.on_state_changed(
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect()

    async def _message_receive_loop(self):
        try:
            async for message_str in self.ws:
                await self.on_message(message_str)
        except ConnectionClosed:
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
                    {"state": PairingState.CONNECTING.value}
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
                    {"state": PairingState.GETTING_TOKEN.value}
                )
                logger.debug("%s: V2 Getting authorization token...", self.controller.serial)
                await self.get_access_token()

                await self.on_state_changed(
                    {"state": PairingState.PAIRING.value}
                )
                logger.debug("%s: V2 Sending pairing code...", self.controller.serial)
                await self.send_pairing_code()

                await self.on_state_changed(
                    {"state": PairingState.CONNECTING.value}
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
        if not self.is_command_allowed(cmd):
            return None, None
        if cmd == Command.BACK and self.is_search_opened:
            return "JD_CancelKeyboard_PhoneCommandData", {}
        if self._input_commands is not None and cmd in V2_INPUT_MAPPING_KEYS:
            data = self._input_commands[cmd].copy()
            return data.pop("__class"), data
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
