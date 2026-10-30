from .abstract_game_wrapper import AbstractGameWrapper
import asyncio
import aiohttp
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time
from urllib.parse import urlparse
from websockets.exceptions import ConnectionClosed, InvalidStatus, NegotiationError
from websockets.protocol import State
from joydance.constants import (
    ACCEL_ACQUISITION_FREQ_HZ,
    ACCEL_ACQUISITION_LATENCY,
    ACCEL_MAX_RANGE,
    FRAME_DURATION,
    UBI_APP_ID,
    UBI_SKU_ID,
    JDMO_PAIRING_URL,
    PairingMethod,
    PairingState,
    Command,
    UiLayer,
    JD_HANDSHAKE_TIMEOUT,
    JD_MAX_PROTOCOL_VERSION,
    JD_SERVICE_TIMEOUT,
)
from joydance.utils import GameConnectionError

logger = logging.getLogger("joydance")


@dataclass
class UiInputState:
    enabled: bool = False
    carousel_enabled: bool = True
    shortcuts: set[Command] = field(default_factory=set)


class JustDanceGameAbstract(AbstractGameWrapper, ABC):
    """
    Abstract base class for Just Dance game protocol logic (v1/v2).
    Implements all logic common to both protocol versions.
    Protocol-specific logic must be implemented in subclasses.
    """

    @staticmethod
    def _websocket_is_open(websocket):
        return websocket is not None and websocket.state is State.OPEN

    def __init__(
        self,
        controller,
        pairing_code=None,
        host_ip_addr=None,
        console_ip_addr=None,
        accel_acquisition_freq_hz=ACCEL_ACQUISITION_FREQ_HZ,
        accel_acquisition_latency=ACCEL_ACQUISITION_LATENCY,
        accel_max_range=ACCEL_MAX_RANGE,
        on_state_changed=None,
        on_game_message=None,
        pairing_method=PairingMethod.DEFAULT.value,
    ):
        super().__init__(on_state_changed, on_game_message)
        self.controller = controller
        self.pairing_code = pairing_code
        self.pairing_method = pairing_method
        self.host_ip_addr = host_ip_addr
        self.console_ip_addr = console_ip_addr
        self.tls_certificate = None
        self.accel_acquisition_freq_hz = accel_acquisition_freq_hz
        self.accel_acquisition_latency = accel_acquisition_latency
        self.accel_max_range = accel_max_range
        self.number_of_accels_sent = 0
        self._scoring_session_id = 0
        self.should_start_accelerometer = False
        self.is_search_opened = False
        self._reset_input_state()
        self.profile_data = {}
        self.accel_data = []
        self.ws = None
        self.is_connected = False
        self._handshake_deadline = None
        self.headers = {
            "Ubi-AppId": UBI_APP_ID,
            "X-SkuId": UBI_SKU_ID,
        }
        self.console_conn = None
        self.reconnection_task = None
        self.pair_task = None
        self._rumble_task = None
        self._connecting_led_task = None
        self.should_reconnect = True
        self.requires_punch_pairing = False

    def _reset_input_state(self):
        self.is_search_opened = False
        self._active_ui_layer = UiLayer.MAIN
        # Keep the underlying screen's controls intact while a popup has focus.
        self._input_states = {layer: UiInputState() for layer in UiLayer}
        self.is_shortcuts_allowed = True
        self.is_lobby_start_allowed = False
        self._has_lobby_start_button = False
        self._lobby_open = False

    @property
    def is_input_allowed(self):
        state = self._input_states[self._active_ui_layer]
        return state.enabled and state.carousel_enabled

    @is_input_allowed.setter
    def is_input_allowed(self, enabled):
        self._input_states[self._active_ui_layer].enabled = enabled

    @property
    def available_shortcuts(self):
        return self._input_states[self._active_ui_layer].shortcuts

    @available_shortcuts.setter
    def available_shortcuts(self, shortcuts):
        self._input_states[self._active_ui_layer].shortcuts = shortcuts

    def _sync_controller_shortcuts(self):
        if hasattr(self.controller, "available_shortcuts"):
            self.controller.available_shortcuts = self.available_shortcuts.copy()

    @property
    def is_in_lobby(self):
        return self._lobby_open and self._active_ui_layer is UiLayer.MAIN

    @is_in_lobby.setter
    def is_in_lobby(self, in_lobby):
        self._lobby_open = in_lobby

    def is_command_allowed(self, cmd):
        if self.should_start_accelerometer:
            return cmd == Command.PAUSE
        # The app's keyboard Cancel action is independent of navigation permissions.
        if self.is_search_opened and cmd == Command.BACK:
            return True
        if not self._input_states[self._active_ui_layer].enabled:
            return False
        if cmd == Command.ACCEPT and self.is_in_lobby:
            return self._has_lobby_start_button and self.is_lobby_start_allowed
        if cmd in {
            Command.UP, Command.RIGHT, Command.DOWN, Command.LEFT,
            Command.ACCEPT, Command.V1_KEYBOARD_ERROR_OK, Command.NEXT_ACTION,
        }:
            return self.is_input_allowed
        if cmd == Command.PAUSE:
            return Command.PAUSE in self.available_shortcuts or (
                self.is_input_allowed and self.is_shortcuts_allowed
            )
        return self.is_shortcuts_allowed and (
            cmd in self.available_shortcuts or (cmd == Command.BACK and self.is_input_allowed)
        )

    async def get_access_token(self):
        headers = {
            "Authorization": "UbiMobile_v1 t=NTNjNWRjZGMtZjA2Yy00MTdmLWJkMjctOTNhZTcxNzU1OTkyOlcwM0N5eGZldlBTeFByK3hSa2hhQ05SMXZtdz06UjNWbGMzUmZaVzB3TjJOYTpNakF5TVMweE1DMHlOMVF3TVRvME5sbz0=",
            "Ubi-AppId": UBI_APP_ID,
            "User-Agent": "UbiServices_SDK_Unity_Light_Mobile_2018.Release.16_ANDROID64_dynamic",
            "Ubi-RequestedPlatformType": "ubimobile",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
            async with session.post(
                "https://public-ubiservices.ubi.com/v1/profiles/sessions",
                json={},
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    raise GameConnectionError(
                        f"Pairing authentication failed (HTTP {resp.status}).",
                        PairingState.ERROR_CONNECTION,
                        should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                    )
                json_body = await resp.json()
                if not isinstance(json_body, dict) or not isinstance(json_body.get("ticket"), str) or not json_body["ticket"]:
                    raise GameConnectionError("Invalid authentication response from the pairing service.", PairingState.ERROR_CONNECTION)
                self.headers["Authorization"] = "Ubi_v1 " + json_body["ticket"]

    async def send_pairing_code(self):
        if self.pairing_method == PairingMethod.JDMO.value:
            json_body = await self._request_jdmo_pairing("pairing-info")
        else:
            url = "https://prod.just-dance.com/sessions/v1/pairing-info"
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
                async with session.get(url, params={"code": self.pairing_code}, ssl=False) as resp:
                    if resp.status != 200:
                        invalid_code = resp.status in {400, 404, 410}
                        raise GameConnectionError(
                            "Invalid or expired pairing code." if invalid_code else f"Pairing service rejected the request (HTTP {resp.status}).",
                            PairingState.ERROR_INVALID_PAIRING_CODE if invalid_code else PairingState.ERROR_CONNECTION,
                            should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                        )
                    json_body = await resp.json()
        if not isinstance(json_body, dict) or not isinstance(json_body.get("pairingUrl"), str):
            raise GameConnectionError("Invalid console address from the pairing service.", PairingState.ERROR_CONNECTION)
        try:
            uri = urlparse(json_body["pairingUrl"])
            if uri.scheme not in {"http", "https", "ws", "wss"} or not uri.hostname or uri.username or uri.password or uri.fragment:
                raise ValueError("Invalid pairing URL")
            uri.port  # Validate the port before passing the address to websockets.
        except ValueError as error:
            raise GameConnectionError("Invalid console address from the pairing service.", PairingState.ERROR_CONNECTION) from error
        path = uri.path.rstrip("/")
        if not path.endswith("/smartphone"):
            path += "/smartphone"
        self.pairing_url = uri._replace(scheme={"http": "ws", "https": "wss"}.get(uri.scheme, uri.scheme), path=path).geturl()
        self.tls_certificate = json_body.get("tlsCertificate")
        self.requires_punch_pairing = json_body.get("requiresPunchPairing", False) in (True, 1, "1", "true")

    async def _request_jdmo_pairing(self, path, payload=None):
        is_punch = payload is not None
        state = PairingState.ERROR_PUNCH_PAIRING if is_punch else PairingState.ERROR_CONNECTION
        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
                for version in (1, 2):
                    url = f"{JDMO_PAIRING_URL}/v{version}/{path}"
                    async with session.request(
                        "POST" if is_punch else "GET", url,
                        params=None if is_punch else {"code": self.pairing_code}, json=payload,
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

    async def send_initiate_punch_pairing(self):
        url = "https://prod.just-dance.com/sessions/v1/initiate-punch-pairing"
        json_payload = {
            "pairingCode": self.pairing_code,
            "mobileIP": self.host_ip_addr,
            "mobilePort": self.host_port,
        }

        if self.pairing_method == PairingMethod.JDMO.value:
            await self._request_jdmo_pairing("initiate-punch-pairing", json_payload)
            return

        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
                async with session.post(url, json=json_payload, ssl=False) as resp:
                    body = await resp.text()
                    if resp.status != 200 or body.strip() != "OK":
                        raise GameConnectionError(
                            f"The pairing service could not request a console connection (HTTP {resp.status}).",
                            PairingState.ERROR_PUNCH_PAIRING,
                            should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                        )
        except (aiohttp.ClientError, TimeoutError) as error:
            raise GameConnectionError(
                f"Could not request the console connection: {str(error) or 'request timed out'}.",
                PairingState.ERROR_PUNCH_PAIRING,
            ) from error

    async def _handle_connection_error(self, error):
        if isinstance(error, GameConnectionError):
            state = error.state
            should_reconnect = self.should_reconnect and error.should_reconnect
        else:
            current_state = self._state.get("state")
            state = PairingState.ERROR_CONSOLE_CONNECTION
            if current_state in {PairingState.GETTING_TOKEN.value, PairingState.PAIRING.value}:
                state = PairingState.ERROR_CONNECTION
            elif current_state is not None and current_state >= PairingState.ERROR_CONTROLLER.value:
                state = PairingState(current_state)
            should_reconnect = self.should_reconnect
        details = str(error) or ("Pairing service request timed out." if state is PairingState.ERROR_CONNECTION else "Game connection timed out.")
        if isinstance(error, InvalidStatus):
            status = error.response.status_code
            details = f"The game rejected the WebSocket connection (HTTP {status})."
            should_reconnect = should_reconnect and (status >= 500 or status in {408, 429})
        elif isinstance(error, NegotiationError):
            details = "Game protocol negotiation failed. Check the selected pairing method."
            should_reconnect = False
        elif isinstance(error, ConnectionClosed):
            if error.rcvd is not None and error.rcvd.code == 4004:
                details = f"The game rejected this controller (code 4004): {error.rcvd.reason or 'no reason supplied'}."
                should_reconnect = False
            elif error.sent is not None and error.sent.reason == "keepalive ping timeout":
                details = "Game connection lost: no response to keepalive checks."
        logger.warning("%s: %s", self.controller.serial, details)
        await self.disconnect(should_reconnect=should_reconnect, error_state={"state": state.value, "error_details": details})

    async def send_message(self, __class, data={}):
        if __class != "JD_PhoneScoringData":
            logger.debug(
                "%s: >>> %s %s", self.controller.serial, __class,
                "[redacted]" if __class == "JD_SubmitKeyboard_PhoneCommandData" else data,
            )
        msg = {"root": {"__class": __class}}
        if data:
            msg["root"].update(data)
        try:
            if self._websocket_is_open(self.ws):
                await self.ws.send(json.dumps(msg, separators=(",", ":")))
                return True
            else:
                logger.warning(
                    "%s: WebSocket is not connected or closed. Cannot send message: %s",
                    self.controller.serial,
                    __class,
                )
        except Exception as error:
            await self._handle_connection_error(error)
        return False

    async def collect_accelerometer_data(self):
        if not self.is_connected:
            return
        if not self.should_start_accelerometer:
            self.accel_data = []
            return
        try:
            accels = self.controller.get_accel_events()
            self.accel_data += accels
        except OSError:
            await self.disconnect()
            return

    async def send_accelerometer_data(self, frames):
        if not self.should_start_accelerometer:
            return
        if frames < 3:
            return
        tmp_accel_data, self.accel_data = self.accel_data, []
        scoring_session_id = self._scoring_session_id
        for start in range(0, len(tmp_accel_data), 10):
            batch = tmp_accel_data[start:start + 10]
            await self.send_message(
                "JD_PhoneScoringData",
                {
                    "accelData": batch,
                    "timeStamp": self.number_of_accels_sent,
                },
            )
            # Scoring may stop or restart while the send yields.
            if not self.should_start_accelerometer or scoring_session_id != self._scoring_session_id:
                return
            self.number_of_accels_sent += len(batch)

    async def sleep_approx(self, target_duration):
        tmp_duration = target_duration
        x = 0.3
        start = time.time()
        while True:
            tmp_duration = tmp_duration * x
            await asyncio.sleep(tmp_duration)
            dt = time.time() - start
            if dt >= target_duration:
                break
            tmp_duration = target_duration - dt

    async def _message_receive_loop(self):
        async for message_str in self.ws:
            await self.on_message(message_str)

    async def tick(self):
        sleep_duration = FRAME_DURATION
        frames = 0
        while True:
            logger.debug(
                "%s %s: Tick controller_is_connected - %s game_is_connected - %s start_accel - %s",
                id(self.controller),
                self.controller.serial,
                self.controller.is_connected(),
                self.is_connected,
                self.should_start_accelerometer,
            )
            # Disconnect if controller is not connected
            if not self.controller.is_connected():
                await self.disconnect(should_reconnect=True)
                return
            # Break if game is not connected
            if not self.is_connected:
                if self._handshake_deadline is not None and asyncio.get_running_loop().time() >= self._handshake_deadline:
                    raise GameConnectionError(f"The game did not finish the handshake within {JD_HANDSHAKE_TIMEOUT} seconds.")
                await asyncio.sleep(sleep_duration)
                continue
            if not self.should_start_accelerometer:
                frames = 0
                await asyncio.sleep(sleep_duration)
                continue
            last_time = time.time()
            frames = frames + 1 if frames < 3 else 1
            # Collecting and sending accelerometer data
            await asyncio.gather(
                self.sleep_approx(sleep_duration),
                self.collect_accelerometer_data(),
            )
            await self.send_accelerometer_data(frames)
            dt = time.time() - last_time
            sleep_duration = max(0, FRAME_DURATION - (dt - sleep_duration))

    async def send_hello(self):
        logger.debug("%s: Pairing...", self.controller.serial)
        await self.send_message(
            "JD_PhoneDataCmdHandshakeHello",
            {
                "clientVersion": "0.1",
                "accelAcquisitionFreqHz": float(self.accel_acquisition_freq_hz),
                "accelAcquisitionLatency": float(self.accel_acquisition_latency),
                "accelMaxRange": float(self.accel_max_range),
            },
        )

    async def start_pair(self):
        await self.stop_pair()
        self._state["connection_requested"] = True
        self.pair_task = asyncio.create_task(self._pair_with_connecting_led())
        self.pair_task.add_done_callback(self._clear_pair_task)
        return self.pair_task

    async def _pair_with_connecting_led(self):
        self._connecting_led_task = asyncio.create_task(self.controller.animate_connecting_led())
        try:
            await self.pair()
        finally:
            if await self._stop_connecting_led() and self._state.get("connection_requested"):
                await self.controller.set_player_led(0, flash=True)

    async def _stop_connecting_led(self):
        task, self._connecting_led_task = self._connecting_led_task, None
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def _clear_pair_task(self, task):
        if self.pair_task is task:
            self.pair_task = None

    async def stop_pair(self):
        task = self.pair_task
        if not task or task.done() or task is asyncio.current_task():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.debug("%s: Pairing task cancelled.", self.controller.serial)

    async def run_connection_tasks(self):
        tasks = set()
        self._handshake_deadline = asyncio.get_running_loop().time() + JD_HANDSHAKE_TIMEOUT
        try:
            try:
                async with asyncio.timeout_at(self._handshake_deadline):
                    await self.send_hello()
            except TimeoutError as error:
                raise GameConnectionError(f"The game did not finish the handshake within {JD_HANDSHAKE_TIMEOUT} seconds.") from error
            if not self._websocket_is_open(self.ws):
                return
            tasks = {
                asyncio.create_task(self._message_receive_loop()),
                asyncio.create_task(self.tick()),
                asyncio.create_task(self.send_command()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

            for task in done:
                task.result()

            if self.ws:
                close_code = getattr(self.ws, "close_code", None)
                if close_code is not None:
                    raise GameConnectionError(
                        f"The game closed the connection (code {close_code}): {self.ws.close_reason or 'no reason supplied'}.",
                        should_reconnect=close_code != 4004,
                    )
                await self.disconnect(should_reconnect=self.should_reconnect)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._stop_rumble()

    async def _play_rumble(self, sound_index):
        try:
            await self.controller.handle_rumble_on_sound_index(sound_index)
        except Exception:
            logger.exception("%s: Error playing rumble.", self.controller.serial)

    async def _stop_rumble(self):
        task, self._rumble_task = self._rumble_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def disconnect(self, should_reconnect=True, error_state=None):
        self.should_reconnect = should_reconnect
        self._state["connection_requested"] = should_reconnect
        if not should_reconnect:
            await self.stop_reconnection()
            # An error may come from a worker owned by pair_task; closing its socket stops that task.
            if error_state is None:
                await self.stop_pair()

        was_connected = self.is_connected
        old_ws = self.ws
        self.ws = None
        old_console_conn, self.console_conn = self.console_conn, None
        had_ws = old_ws is not None
        self.is_connected = False
        self._handshake_deadline = None
        # A new connection may open a pause menu without repeating the stop-scoring message.
        self.should_start_accelerometer = False
        self.controller.set_accel_collection(False)
        self._reset_input_state()
        self._sync_controller_shortcuts()
        self.accel_data.clear()

        start_reconnecting = (
            self.should_reconnect
            and not self.reconnection_task
            and (was_connected or had_ws or error_state is not None or not self.controller.is_connected())
        )

        # Finish closing and notifying even if the owning connection loop is cancelled.
        cleanup = asyncio.create_task(
            self._finish_disconnect(old_ws, was_connected or not should_reconnect, old_console_conn, error_state)
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if start_reconnecting and self.should_reconnect and not self.reconnection_task:
            self.reconnection_task = asyncio.create_task(self.attempt_reconnect())
        if cancelled:
            raise asyncio.CancelledError

    async def _finish_disconnect(self, old_ws, notify_disconnected, old_console_conn, error_state=None):
        try:
            if old_ws is not None and old_ws.state is not State.CLOSED:
                await old_ws.close()
        finally:
            await self._stop_rumble()
            if old_console_conn is not None:
                old_console_conn.close()
            was_connecting = await self._stop_connecting_led()
            if notify_disconnected or was_connecting:
                await self.controller.set_player_led(0, flash=True)
            if notify_disconnected or error_state is not None:
                logger.debug("%s: Disconnected", self.controller.serial)
                await self.on_state_changed(
                    error_state if error_state is not None else {"state": PairingState.DISCONNECTED.value}
                )

    async def attempt_reconnect(self):
        retry_delay = self.reconnection_start_retry_delay
        while self.should_reconnect and not self.is_connected:
            try:
                # Reconnect controller if it is not connected
                is_controller_reconnected = True
                if not self.controller.is_connected():
                    is_controller_reconnected = await self.controller.reconnect()
                # Pair with the game
                if is_controller_reconnected and not self.is_connected and (
                    not self.pair_task or self.pair_task.done()
                ):
                    await self.start_pair()
            except Exception:
                logger.exception("%s: Reconnection attempt failed", self.controller.serial)

            if not self.should_reconnect:
                break
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, self.reconnection_max_retry_delay)
        self.reconnection_task = None

    async def stop_reconnection(self):
        self.should_reconnect = False
        if self.reconnection_task:
            self.reconnection_task.cancel()
            try:
                await self.reconnection_task
            except asyncio.CancelledError:
                logger.debug("%s: Reconnection task cancelled.", self.controller.serial)
            self.reconnection_task = None

    async def parse_profile_data(self, profile_data_msg):
        player_id = profile_data_msg.get("playerId")
        if player_id is not None:
            player_id += 1
        color = profile_data_msg.get("color")
        if color is not None:
            color = [int(c * 255) for c in color]

        self.profile_data = {
            "player_name": profile_data_msg.get("name"),
            "player_id": player_id,
            "player_color": color,
            "player_image": profile_data_msg.get("image"),
            "skin_image": profile_data_msg.get("skinImage"),
            "additional_message": profile_data_msg.get("additionalMessage"),
        }
        await self.on_state_changed(self.profile_data)

    async def parse_input_setup_data(self, message):
        # Discard queued presses so enabling input requires a fresh press.
        self.controller.get_latest_command(commands_to_check=set())
        layer = UiLayer.POPUP if message.get("applyOnPopup") else UiLayer.MAIN
        self._input_states[layer].enabled = bool(message.get("isEnabled"))

    async def parse_phone_setup_data(self, message):
        setup_data = message.get("setupData")
        if not isinstance(setup_data, dict):
            # The app keeps the screen when only input setup or metadata is supplied.
            input_setup = message.get("inputSetup")
            if isinstance(input_setup, dict):
                await self.parse_input_setup_data(input_setup)
            return
        # Discard queued presses so they cannot act on the new screen.
        self.controller.get_latest_command(commands_to_check=set())
        layer = UiLayer.POPUP if message.get("isPopup") else UiLayer.MAIN
        if message.get("isPopup"):
            self.is_search_opened = False
        state = UiInputState(
            enabled=(
                bool(message.get("inputSetup", {}).get("isEnabled"))
                and setup_data.get("__class") != "JD_SimplePhoneUiData"
            ),
            carousel_enabled=bool(setup_data.get("mainCarousel", {}).get("isEnabled", True)),
        )
        if setup_data.get("gameplaySetup", {}).get("pauseSlider", {}):
            state.shortcuts.add(Command.PAUSE)
        self._input_states[layer] = state
        self._active_ui_layer = layer
        if layer is UiLayer.MAIN:
            self._lobby_open = bool(setup_data.get("lobbySetup"))
            start_button = setup_data.get("lobbySetup", {}).get("startButton")
            self._has_lobby_start_button = start_button is not None
            self.is_lobby_start_allowed = (
                self._has_lobby_start_button and bool(start_button.get("isEnabled", True))
            )
        self._sync_controller_shortcuts()

    async def on_message(self, message_str):
        message = json.loads(message_str)
        __class = message.get("__class")

        if __class != "JD_PhoneUiSetupData" or message.get("isPopup"):
            logger.debug(
                "%s-%s: <<< %s",
                self.controller.serial,
                self.profile_data.get("player_id", "N/A"),
                message,
            )
        else:
            logger.debug(
                "%s-%s: <<< JD_PhoneUiSetupData (keys: %s)",
                self.controller.serial,
                self.profile_data.get("player_id", "N/A"),
                list(message.keys()),
            )

        if __class == "JD_PhoneDataCmdHandshakeContinue":
            protocol_version = message.get("protocolVersion")
            if protocol_version is not None and (
                type(protocol_version) is not int or not 0 <= protocol_version <= JD_MAX_PROTOCOL_VERSION
            ):
                raise GameConnectionError(
                    f"Unsupported game protocol version: {protocol_version}. JoyDance supports versions through {JD_MAX_PROTOCOL_VERSION}.",
                    should_reconnect=False,
                )
            await self.send_message("JD_PhoneDataCmdSync", {"phoneID": message["phoneID"]})
        elif __class == "JD2015_NotPhoneScoring":
            raise GameConnectionError("Phone scoring is not available in this game session.", should_reconnect=False)
        elif __class == "JD_ProfilePhoneUiData":
            previous_player_id = self.profile_data.get("player_id")
            await self.parse_profile_data(message)
            player_id = self.profile_data.get("player_id")
            if player_id is not None and (
                player_id != previous_player_id or self._connecting_led_task is not None
            ):
                was_connecting = await self._stop_connecting_led()
                await self.controller.set_player_led(player_id, flash=was_connecting)
        elif __class == "JD_PlaySound_ConsoleCommandData":
            sound_index = message.get("soundIndex", 0)
            await self._stop_rumble()
            self._rumble_task = asyncio.create_task(self._play_rumble(sound_index))
        elif __class == "JD_PhoneDataCmdSyncEnd":
            if not await self.send_message("JD_PhoneDataCmdSyncEnd", {"phoneID": message["phoneID"]}):
                return
            self._handshake_deadline = None
            if not self.is_connected:
                self.is_connected = True
                await self.on_state_changed({"state": PairingState.CONNECTED.value, "error_details": None})
        elif __class == "JD_EnableAccelValuesSending_ConsoleCommandData":
            self._scoring_session_id += 1
            self.accel_data.clear()
            self.controller.set_accel_collection(True)
            self.should_start_accelerometer = True
            self.number_of_accels_sent = 0
        elif __class == "JD_DisableAccelValuesSending_ConsoleCommandData":
            self.accel_data.clear()
            self.controller.set_accel_collection(False)
            self.should_start_accelerometer = False
        elif __class == "InputSetup_ConsoleCommandData":
            await self.parse_input_setup_data(message)
        elif __class == "EnableCarousel_ConsoleCommandData":
            # Discard queued presses so enabling input cannot replay an old action.
            self.controller.get_latest_command(commands_to_check=set())
            layer = UiLayer.POPUP if message.get("applyOnPopup") else UiLayer.MAIN
            state = self._input_states[layer]
            state.carousel_enabled = bool(message.get("isEnabled"))
        elif __class == "JD_EnableLobbyStartbutton_ConsoleCommandData":
            # Discard queued confirmations so enabling Start cannot launch the song.
            self.controller.get_latest_command(commands_to_check=set())
            self.is_lobby_start_allowed = bool(message.get("isEnabled"))
        elif __class == "ShortcutSetup_ConsoleCommandData":
            # Discard queued presses so enabling shortcuts requires a fresh press.
            self.controller.get_latest_command(commands_to_check=set())
            # The app shares this flag between the main screen and its popup.
            self.is_shortcuts_allowed = bool(message.get("isEnabled"))
        elif __class == "JD_PhoneUiShortcutData":
            # Discard queued presses so they cannot trigger newly assigned shortcuts.
            self.controller.get_latest_command(commands_to_check=set())
            shortcuts = set()
            for item in message.get("shortcuts", []):
                if item["__class"] == "JD_PhoneAction_Shortcut":
                    try:
                        shortcuts.add(Command(item["shortcutType"]))
                    except (KeyError, ValueError):
                        logger.warning(
                            "Unknown or invalid ShortcutType: %s in %s",
                            item.get("shortcutType"),
                            item,
                        )
            layer = self._active_ui_layer
            if "applyOnPopup" in message:
                layer = UiLayer.POPUP if message["applyOnPopup"] else UiLayer.MAIN
            self._input_states[layer].shortcuts = shortcuts
            self._sync_controller_shortcuts()
        elif __class == "JD_OpenPhoneKeyboard_ConsoleCommandData":
            # Require a fresh press before allowing keyboard cancellation.
            self.controller.get_latest_command(commands_to_check=set())
            self.is_search_opened = True
        elif __class == "JD_CancelKeyboard_ConsoleCommandData":
            # Discard keyboard presses before returning to the underlying screen.
            self.controller.get_latest_command(commands_to_check=set())
            self.is_search_opened = False
        elif __class == "JD_ClosePopup_ConsoleCommandData":
            # Discard queued popup presses so they cannot act on the underlying screen.
            self.controller.get_latest_command(commands_to_check=set())
            self.is_search_opened = False
            self._active_ui_layer = UiLayer.MAIN
            self._sync_controller_shortcuts()
        elif __class == "JD_PhoneUiSetupData":
            await self.parse_phone_setup_data(message)

        await self.on_game_message(message)

    def _get_button_command(self, commands):
        return self.controller.get_latest_command(commands_to_check=commands)

    async def send_command(self):
        next_joystick_repeat_time = 0
        last_joystick_command = None
        while True:
            if not self.is_connected:
                if not self._websocket_is_open(self.ws):
                    return
                # Discard presses made before pairing finishes so they cannot replay on connect.
                self.controller.get_latest_command(commands_to_check=set())
                last_joystick_command = None
                await asyncio.sleep(FRAME_DURATION)
                continue
            try:
                await asyncio.sleep(FRAME_DURATION)
                commands = {cmd for cmd in Command if self.is_command_allowed(cmd)}
                button_command = self._get_button_command(commands)

                joystick_command = None
                if self.is_input_allowed and not self.should_start_accelerometer:
                    joystick_command = self.controller.get_joystick_command()
                if joystick_command != last_joystick_command:
                    next_joystick_repeat_time = 0
                last_joystick_command = joystick_command

                # Button presses are discrete events; only a held stick needs a repeat delay.
                cmd = button_command
                if (
                    not cmd and joystick_command in commands
                    and time.monotonic() >= next_joystick_repeat_time
                ):
                    cmd = joystick_command

                if cmd and cmd in commands:
                    logger.debug("%s: raw cmd: %s", self.controller.serial, cmd)
                    try:
                        __class, data = await self.preprocess_command(cmd)
                        logger.debug(
                            "%s: preprocessed cmd: %s %s",
                            self.controller.serial,
                            __class,
                            data,
                        )
                    except Exception:
                        logger.exception(
                            "%s: An error occurred while processing command: %s",
                            self.controller.serial,
                            cmd,
                        )
                        continue

                    if __class is None:
                        continue

                    if self.is_command_allowed(cmd):
                        logger.debug("%s: >>> %s %s", self.controller.serial, __class, data)
                        await self.send_message(__class, data)
                        if not button_command:
                            next_joystick_repeat_time = time.monotonic() + FRAME_DURATION * 15
            except Exception:
                logger.exception(
                    "%s: An error occurred in send_command loop.",
                    self.controller.serial,
                )
                await self.disconnect()

    @abstractmethod
    async def connect_ws(self):
        raise NotImplementedError

    @abstractmethod
    async def pair(self):
        raise NotImplementedError

    @abstractmethod
    async def preprocess_command(self, cmd):
        raise NotImplementedError
