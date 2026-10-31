import logging
import json
import time
from urllib.parse import urlparse
import ssl
from websockets.asyncio.client import connect

from joydance.constants import (
    Command,
    PairingState,
    PairingMethod,
    WS_SUBPROTOCOLS,
    WsSubprotocolVersion,
    V2_INPUT_MAPPING_KEYS,
    V2_TAKEOVER_HOLD_DURATION,
    JD_PING_INTERVAL,
    JD_PING_TIMEOUT,
)
from .justdance_abstract import JustDanceGameAbstract
from .discovery import discover_jdmo_game
from .websocket_connection import JustDanceConnection
from joydance.utils.network import is_private_ipv4


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
        if cmd == Command.BACK and self.is_search_opened:
            return True
        state = self._input_states[self._active_ui_layer]
        if cmd in state.button_commands:
            return state.button_commands[cmd] is not None
        if cmd in state.shortcut_commands:
            return state.shortcut_commands[cmd] is not None
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
            "JD_EnableAccelValuesSending_ConsoleCommandData",
        } or (
            message_class == "JD_PhoneUiSetupData" and (
                isinstance(message.get("setupData"), dict) or isinstance(message.get("inputSetup"), dict)
            )
        ):
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

    def _parse_shortcuts(self, message, state):
        state.shortcut_commands = {}
        state.shortcuts &= {Command.PAUSE}
        items = message.get("shortcuts", [])
        items = list(items) if isinstance(items, list) else []
        if "navigationBar" in message:
            navigation = message["navigationBar"]
            navigation = navigation if isinstance(navigation, list) else []
            back = navigation[0] if navigation else None
            state.shortcut_commands[Command.BACK] = self._parse_command(back.get("command")) if isinstance(back, dict) else None
            if back is not None:
                state.shortcuts.add(Command.BACK)
            items.extend(navigation[1:])
        for item in items:
            if not isinstance(item, dict):
                continue
            command = self._parse_command(item.get("command"))
            identifier = item.get("shortcutType")
            if identifier is None and command:
                identifier = command.get("identifier", command.get("input"))
            if identifier is None:
                continue
            try:
                cmd = Command(identifier)
            except (TypeError, ValueError):
                logger.warning("Unknown V2 shortcut: %s", identifier)
                continue
            state.shortcuts.add(cmd)
            # Metadata-only updates retain legacy bindings; explicit empty commands disable them.
            if "command" in item:
                state.shortcut_commands[cmd] = command

    async def parse_phone_setup_data(self, message):
        await super().parse_phone_setup_data(message)
        setup = message.get("setupData")
        if not isinstance(setup, dict):
            return
        state = self._input_states[self._active_ui_layer]
        self._parse_shortcuts(setup, state)
        self._sync_controller_shortcuts()
        for cmd, button in (
            (Command.ACCEPT, setup.get("lobbySetup", {}).get("startButton")),
            (Command.PAUSE, setup.get("gameplaySetup", {}).get("pauseSlider")),
        ):
            if not isinstance(button, dict) or not ("actions" in button or "command" in button):
                continue
            actions = button.get("actions")
            action = actions[0] if isinstance(actions, list) and actions else None
            if action is None:
                action = button
            state.button_commands[cmd] = self._parse_command(action.get("command")) if isinstance(action, dict) else None

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
            command = self._parse_command(encoded)
            if command is not None:
                self._input_commands[cmd] = command

    async def send_pairing_code(self):
        if self.pairing_method == PairingMethod.JDMO.value:
            address = await discover_jdmo_game(self.pairing_code, self.host_ip_addr)
            if address:
                # shortcut: local JDMO 2021 PC uses plaintext V2 on 8080; revisit if its listener changes.
                self.pairing_url = f"ws://{address}:8080/smartphone"
                self.tls_certificate = None
                self.requires_punch_pairing = False
                logger.debug("%s: Found matching JDMO game at %s", self.controller.serial, address)
                return
        await super().send_pairing_code()

    async def connect_ws(self):
        try:
            uri = urlparse(self.pairing_url)
            server_hostname = None
            ssl_context = None
            if uri.scheme == "wss":
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_context.set_ciphers("ALL")
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                if self.tls_certificate:
                    try:
                        ssl_context.load_verify_locations(cadata=self.tls_certificate)
                    except Exception as e:
                        logger.error("%s: V2 Failed to load TLS certificate: %s", self.controller.serial, e)

                server_hostname = uri.hostname
                if self.console_conn and server_hostname and is_private_ipv4(server_hostname):
                    # Use the accepted peer for private addresses advertised behind NAT.
                    server_hostname = self.console_conn.getpeername()[0]

            subprotocol = WS_SUBPROTOCOLS[WsSubprotocolVersion.V2.value]
            logger.debug(
                f"{self.controller.serial}: V2 connecting to {self.pairing_url} with subprotocol {subprotocol}, server_hostname: {server_hostname}"
            )
            async with connect(
                self.pairing_url,
                subprotocols=[subprotocol],
                sock=self.console_conn,
                ssl=ssl_context,
                ping_interval=JD_PING_INTERVAL,
                ping_timeout=JD_PING_TIMEOUT,
                create_connection=JustDanceConnection,
                max_size=None,  # Disable message size limit to handle large JD_PhoneUiSetupData messages
                server_hostname=server_hostname,
                proxy=None,
            ) as websocket:
                self.ws = websocket
                # The game connection is ready only after SyncEnd.
                await self.run_connection_tasks()

        except Exception as e:
            logger.exception(
                "%s: V2 An error occurred while connecting WebSocket to console.",
                self.controller.serial,
            )
            await self._handle_connection_error(e)

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
                if self.pairing_method != PairingMethod.JDMO.value:
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
                    await self.hole_punching(on_listening=self.send_initiate_punch_pairing)

            await self.connect_ws()
        except Exception as error:
            logger.exception("%s: V2 An error occurred while pairing.", self.controller.serial)
            await self._handle_connection_error(error)

    async def preprocess_command(self, cmd):
        if not self.is_command_allowed(cmd):
            return None, None
        if cmd == Command.BACK and self.is_search_opened:
            return "JD_CancelKeyboard_PhoneCommandData", {}
        state = self._input_states[self._active_ui_layer]
        command = state.button_commands.get(cmd) or state.shortcut_commands.get(cmd)
        if command:
            data = command.copy()
            return data.pop("__class"), data
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
