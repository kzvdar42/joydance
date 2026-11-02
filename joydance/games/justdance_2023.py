"""JD23+ hand-device controller protocol (Harbour/QUIC)."""
import asyncio
import ipaddress
import json
import logging
import math
import re
import ssl
import time

from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.packet import QuicProtocolVersion
from aioquic.tls import CipherSuite

from joydance.constants import Command, JD_SERVICE_TIMEOUT, PairingState
from joydance.utils import GameConnectionError
from .harbour_connection import HarbourConnection, HarbourProtocol, ROOT_CA
from .jd23_auth import JD23Pairing
from .justdance_abstract import JustDanceGameAbstract


logger = logging.getLogger(__name__)


SHORTCUTS = {
    "Back": (Command.BACK, "NavigationBack"),
    "QuickChat": (Command.JD23_QUICK_CHAT, "NavigationQuickChat"),
    "GroupBarOrAvatarChange": (Command.JD23_GROUP, "NavigationOpenGroupBar"),
    "Fullscreen": (Command.JD23_FULLSCREEN, "NavigationFullscreen"),
    "SweatMode": (Command.JD23_SWEAT, "NavigationSweat"),
    "DohDecadeSelector": (Command.JD23_DECADES, "NavigationDohDecadeSelector"),
    "DohActivitiesBoard": (Command.JD23_ACTIVITIES, "NavigationDohActivitiesBoard"),
}
DIRECTIONS = {Command.UP: (0, 1), Command.DOWN: (0, -1), Command.LEFT: (-1, 0), Command.RIGHT: (1, 0)}
MENU_FEEDBACK_COMMANDS = {wire_name for _, wire_name in SHORTCUTS.values()} | {"NavigationChangeAvatar"}
SAFE_LOG_PARAMETERS = {"", "true", "false", "0/True", *SHORTCUTS, *(str(n) for n in range(8))}


class JustDanceGame2023(JustDanceGameAbstract):
    def __init__(self, controller, *args, **kwargs):
        super().__init__(controller, *args, **kwargs)
        self.pairing = JD23Pairing()
        self.console_endpoint = None

    def _reset_input_state(self):
        if getattr(self, "_motion_log_time", None) is not None:
            self._log_motion_summary(final=True)
        super()._reset_input_state()
        self._on_hold = False
        self._is_main_player = False
        self._handshake_sent = False
        self._ping_enabled = False
        self._ping_period = 10
        self._last_ping = self._last_pong = time.monotonic()
        self._motion_log_time = None
        self._motion_batches = self._motion_samples = self._motion_empty_frames = 0

    async def pair(self):
        self.is_connected = False
        self.should_reconnect = True
        try:
            await self.on_state_changed({"state": PairingState.GETTING_TOKEN.value})

            async def on_pairing():
                await self.on_state_changed({"state": PairingState.PAIRING.value})

            info = await self.pairing.pair(self.pairing_code, on_pairing)
            address, port = info.get("deviceIP"), info.get("devicePort")
            try:
                peer = ipaddress.ip_address(address)
                if peer.is_unspecified or peer.is_multicast or peer.is_loopback or peer.is_global:
                    raise ValueError("Expected a console on the local network")
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError("Invalid port")
            except (TypeError, ValueError) as error:
                raise GameConnectionError("Invalid JD23+ console endpoint in pairing response.",
                                          PairingState.ERROR_CONNECTION, should_reconnect=False) from error
            self.console_endpoint = (str(peer), port)
            await self.on_state_changed({"state": PairingState.CONNECTING.value})
            await self.connect_ws()
        except (KeyError, TypeError, ValueError):
            await self._handle_connection_error(GameConnectionError(
                "Unexpected JD23+ pairing service response.",
                PairingState.ERROR_CONNECTION, should_reconnect=False,
            ))
        except Exception as error:
            await self._handle_connection_error(error)

    async def connect_ws(self):
        configuration = QuicConfiguration(
            is_client=True, alpn_protocols=["ubisoft.harbour.keel/1.0"],
            supported_versions=[QuicProtocolVersion.VERSION_1],
            connection_id_length=16,  # Match Harbour; JD26 ignores the default eight-byte IDs.
            cipher_suites=[CipherSuite.AES_128_GCM_SHA256, CipherSuite.AES_256_GCM_SHA384],
            verify_mode=ssl.CERT_REQUIRED, cadata=ROOT_CA, idle_timeout=300,
        )
        async with connect(*self.console_endpoint, configuration=configuration,
                           create_protocol=HarbourProtocol, wait_connected=False) as protocol:
            protocol.transmit()
            try:
                async with asyncio.timeout(JD_SERVICE_TIMEOUT):
                    await protocol.wait_connected()
            except (ConnectionError, TimeoutError) as error:
                raise GameConnectionError("JD23+ QUIC connection failed. Check that the console is on the same network.") from error
            reader, writer = await protocol.create_stream()
            connection = HarbourConnection(protocol, reader, writer)
            self.ws = connection
            try:
                await self.run_connection_tasks()
            finally:
                await connection.close()

    async def send_hello(self):
        await self.send_message("Connect_HelloFromClient", {"category": 0})

    async def send_message(self, name, data=None):
        if not self._websocket_is_open(self.ws):
            return False
        data = data or {}
        parameter = data.get("parameter", "")
        messages = [(name, parameter)]
        if name == "NavigationMoveDirection":
            messages = [("NavigationMoveStarted", ""), (name, parameter), ("NavigationMoveEnded", "")]
        for command_name, command_parameter in messages:
            # End a gesture even if the console revokes input while a send yields.
            if command_name == "NavigationMoveDirection" and (
                not self.is_input_allowed or self._on_hold or self.should_start_accelerometer
            ):
                continue
            packet = {"commandCategory": data.get("category", 2), "commandName": command_name,
                      "commandParameter": command_parameter}
            await self.ws.send(json.dumps(packet, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
            self._log_command(">>>", packet["commandCategory"], command_name, command_parameter)
            if command_name in MENU_FEEDBACK_COMMANDS:
                await self._trigger_menu_feedback(command_name)
        return True

    async def _trigger_menu_feedback(self, trigger):
        enabled = self.is_connected and self.controller.rumble_enabled
        logger.debug("%s: JD23+ feedback trigger=%r rumble_enabled=%s",
                     self.controller.serial, trigger, enabled)
        if enabled:
            await self._stop_rumble()
            # Adapt the APK's Validate feedback using the existing short Joy-Con cue.
            self._rumble_task = asyncio.create_task(self._play_rumble(8))

    def _log_command(self, direction, category, name, parameter):
        if (name == "Connect_PingBack" or (name == "Request_Ping" and self._ping_enabled)
                or (direction == ">>>" and name == "MotionScoringData")):
            return
        # Unknown payloads may contain player details, URLs or credentials.
        safe_parameter = parameter if isinstance(parameter, str) and parameter in SAFE_LOG_PARAMETERS else "[redacted]"
        logger.debug("%s: JD23+ %s category=%s command=%r parameter=%r parameter_chars=%s",
                     self.controller.serial, direction, category, name[:128], safe_parameter,
                     len(parameter) if isinstance(parameter, str) else 0)

    def _set_scoring(self, enabled):
        self._log_motion_summary(final=True)
        self._scoring_session_id += 1
        self.should_start_accelerometer = enabled
        self.accel_data.clear()
        self.controller.set_accel_collection(enabled)
        self._motion_log_time = time.monotonic() if enabled else None
        logger.debug("%s: JD23+ scoring enabled=%s session=%s",
                     self.controller.serial, enabled, self._scoring_session_id)

    def _log_motion_summary(self, final=False):
        if self._motion_log_time is None:
            return
        now = time.monotonic()
        elapsed = now - self._motion_log_time
        if not final and elapsed < 1:
            return
        logger.debug("%s: JD23+ motion session=%s interval=%.3fs batches=%s samples=%s "
                     "samples_per_s=%.1f empty_frames=%s final=%s",
                     self.controller.serial, self._scoring_session_id, elapsed,
                     self._motion_batches, self._motion_samples,
                     self._motion_samples / elapsed if elapsed > 0 else 0,
                     self._motion_empty_frames, final)
        self._motion_log_time = None if final else now
        self._motion_batches = self._motion_samples = self._motion_empty_frames = 0

    def _discard_buttons(self):
        self.controller.get_latest_command(commands_to_check=set())

    async def on_message(self, message_str):
        try:
            message = json.loads(message_str)
        except ValueError as error:
            raise GameConnectionError("Invalid JD23+ command JSON.", should_reconnect=False) from error
        if not isinstance(message, dict):
            raise GameConnectionError("Invalid JD23+ command.", should_reconnect=False)
        category, name, param = (message.get(key) for key in ("commandCategory", "commandName", "commandParameter"))
        if type(category) is not int or not isinstance(name, str) or not isinstance(param, (str, type(None))):
            raise GameConnectionError("Invalid JD23+ command fields.", should_reconnect=False)
        param = param or ""
        self._log_command("<<<", category, name, param)
        if category == 0:
            if name == "Connect_HandshakeBackFromServer":
                if param not in ("", "0"):
                    raise GameConnectionError("Unsupported JD23+ handshake version.", should_reconnect=False)
                await self.send_message("Connect_HandshakeOKFromClient", {
                    # This wire field is the UI's UserName, not a Ubisoft profile ID.
                    "category": 0, "parameter": "userId=[Unknown Player]platform=[Android]",
                })
                self._handshake_sent = True
            elif name in {"Connect_HandshakeOKFromServerAsMainPlayer", "Connect_HandshakeOKFromServerAsGuest"}:
                if not self._handshake_sent:
                    raise GameConnectionError("Unexpected JD23+ handshake acknowledgement.", should_reconnect=False)
                fields = re.split(r"[/|]", param)
                if len(fields) < 3 or fields[1].lower() not in {"true", "false"} or fields[2] not in {"0", "1", "HandDevice", "Camera"}:
                    raise GameConnectionError("Invalid JD23+ player acknowledgement.", should_reconnect=False)
                self._is_main_player = name.endswith("AsMainPlayer")
                if len(fields) > 3 and fields[3] not in {"0", "1", "2", "3", "4", "5"}:
                    raise GameConnectionError("Invalid JD23+ player index.", should_reconnect=False)
                player_id = int(fields[3]) + 1 if len(fields) > 3 else 1
                first_acknowledgement = not self.is_connected
                self.is_connected = True
                self._handshake_deadline = None
                self._discard_buttons()
                camera = fields[2] in {"1", "Camera"}
                self._set_scoring(fields[1].lower() == "true" and not camera)
                await self._stop_connecting_led()
                await self.controller.set_player_led(player_id, flash=True)
                await self.on_state_changed({"state": PairingState.CONNECTED.value, "error_details": None,
                                             "player_name": fields[0], "player_id": player_id,
                                             "is_main_player": self._is_main_player})
                if first_acknowledgement:
                    await self._trigger_menu_feedback("ConnectionAcknowledged")
                if camera:
                    await self._choose_hand_device()
            elif name in {"Connect_DeviceConnectionRefusal", "Connect_PlayerRemovalFromFullGroup"}:
                raise GameConnectionError(f"JD23+ refused or removed this controller: {param[:120] or 'no reason supplied'}.",
                                          should_reconnect=False)
        elif category == 1:
            enabled = param == "true"
            if name == "Request_Ping":
                self._ping_enabled = True
                self._last_ping = time.monotonic()
            elif name == "Request_SetLongPing":
                self._ping_period = 100 if enabled else 10
                self._last_ping = time.monotonic()
            elif name in {"Request_EnableNavigation", "Request_EnableWorldNavigation"}:
                self._discard_buttons()
                self.is_input_allowed = enabled
            elif name == "Request_EnableScoring":
                self._discard_buttons()
                self._set_scoring(enabled)
            elif name == "Request_OnHold":
                self._discard_buttons()
                self._on_hold = enabled
            elif name in {"Request_EnableScaShortcutButton", "Request_EnableScaShortcutButtonOnMasterOnly", "Request_DisableScaShortcutButton"}:
                self._discard_buttons()
                shortcut = param
                if param.isdigit() and int(param) < len(SHORTCUTS):
                    shortcut = list(SHORTCUTS)[int(param)]
                if shortcut in SHORTCUTS:
                    cmd = SHORTCUTS[shortcut][0]
                    if name == "Request_DisableScaShortcutButton" or (name.endswith("OnMasterOnly") and not self._is_main_player):
                        self.available_shortcuts.discard(cmd)
                    else:
                        self.available_shortcuts.add(cmd)
                    self._sync_controller_shortcuts()
            elif name in {"EnableScoringSelection ", "DisableCameraScoring"}:
                await self._choose_hand_device()
            elif name == "CalibrateCameraScoring ":
                raise GameConnectionError("JoyDance supports hand-device scoring. Select a controller instead of camera scoring on the console.",
                                          should_reconnect=False)
            elif name == "GameplayQuitRequest":
                self._set_scoring(False)
        await self.on_game_message(message)

    async def _choose_hand_device(self):
        self._set_scoring(False)
        await self.send_message("ScoringModeChange", {"category": 3, "parameter": "0/True"})

    def is_command_allowed(self, cmd):
        if self._on_hold or not self.is_connected:
            return False
        if self.should_start_accelerometer:
            return cmd is Command.PAUSE
        if cmd in DIRECTIONS or cmd is Command.ACCEPT:
            return self.is_input_allowed
        return cmd in self.available_shortcuts

    async def preprocess_command(self, cmd):
        if not self.is_command_allowed(cmd):
            return None, None
        if cmd in DIRECTIONS:
            x, y = DIRECTIONS[cmd]
            return "NavigationMoveDirection", {"parameter": json.dumps({"x": x, "y": y}, separators=(",", ":"))}
        if cmd is Command.ACCEPT:
            return "NavigationValidate", {}
        if cmd is Command.PAUSE:
            return "NavigationPauseGame", {}
        for shortcut, (command, wire_name) in SHORTCUTS.items():
            if cmd is command:
                if shortcut == "GroupBarOrAvatarChange" and not self._is_main_player:
                    wire_name = "NavigationChangeAvatar"
                return wire_name, {}
        return None, None

    async def send_accelerometer_data(self, frames):
        if not self.should_start_accelerometer:
            return
        samples, self.accel_data = self.accel_data, []
        generation = self._scoring_session_id
        if not samples:
            self._motion_empty_frames += 1
        for offset in range(0, len(samples), 10):
            vectors = []
            for sample in samples[offset:offset + 10]:
                if len(sample) != 3 or not all(math.isfinite(value) for value in sample):
                    raise ValueError("Invalid controller accelerometer sample.")
                vectors.append(dict(zip(("x", "y", "z"), sample)))
            if not self.should_start_accelerometer or generation != self._scoring_session_id:
                return
            sent = await self.send_message("MotionScoringData", {"category": 3, "parameter": json.dumps(
                {"AccelerometerData": vectors}, separators=(",", ":"), allow_nan=False,
            )})
            if sent:
                if generation == self._scoring_session_id and self._motion_log_time is not None:
                    self._motion_batches += 1
                    self._motion_samples += len(vectors)
                else:
                    logger.debug("%s: JD23+ motion session=%s completed_after_stop batches=1 samples=%s",
                                 self.controller.serial, generation, len(vectors))
        self._log_motion_summary()

    async def _heartbeat(self):
        while self._websocket_is_open(self.ws):
            now = time.monotonic()
            if self._ping_enabled:
                if now - self._last_ping > self._ping_period:
                    raise GameConnectionError("JD23+ connection lost: the console stopped sending heartbeats.")
                if now - self._last_pong > 1:
                    self._last_pong = now
                    await self.send_message("Connect_PingBack", {"category": 0, "parameter": str(self._ping_period)})
            await asyncio.sleep(0.1)

    async def tick(self):
        tasks = {asyncio.create_task(super().tick()), asyncio.create_task(self._heartbeat())}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
