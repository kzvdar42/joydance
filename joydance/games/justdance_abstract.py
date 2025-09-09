from .abstract_game_wrapper import AbstractGameWrapper
import asyncio
import aiohttp
import json
import logging
from abc import ABC, abstractmethod
import time
import websockets
from joydance.constants import (
    ACCEL_ACQUISITION_FREQ_HZ,
    ACCEL_ACQUISITION_LATENCY,
    ACCEL_MAX_RANGE,
    FRAME_DURATION,
    UBI_APP_ID,
    UBI_SKU_ID,
    PairingState,
    Command,
)

logger = logging.getLogger("joydance")


class JustDanceGameAbstract(AbstractGameWrapper, ABC):
    """
    Abstract base class for Just Dance game protocol logic (v1/v2).
    Implements all logic common to both protocol versions.
    Protocol-specific logic must be implemented in subclasses.
    """

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
    ):
        super().__init__(on_state_changed, on_game_message)
        self.controller = controller
        self.pairing_code = pairing_code
        self.host_ip_addr = host_ip_addr
        self.console_ip_addr = console_ip_addr
        self.tls_certificate = None
        self.accel_acquisition_freq_hz = accel_acquisition_freq_hz
        self.accel_acquisition_latency = accel_acquisition_latency
        self.accel_max_range = accel_max_range
        self.number_of_accels_sent = 0
        self.should_start_accelerometer = False
        self.is_input_allowed = False
        self.profile_data = {}
        self.available_shortcuts = set()
        self.accel_data = []
        self.ws = None
        self.is_connected = False
        self.headers = {
            "Ubi-AppId": UBI_APP_ID,
            "X-SkuId": UBI_SKU_ID,
        }
        self.console_conn = None
        self.is_search_opened = False
        self.reconnection_task = None
        self.should_reconnect = True
        self.requires_punch_pairing = False

    async def get_access_token(self):
        headers = {
            "Authorization": "UbiMobile_v1 t=NTNjNWRjZGMtZjA2Yy00MTdmLWJkMjctOTNhZTcxNzU1OTkyOlcwM0N5eGZldlBTeFByK3hSa2hhQ05SMXZtdz06UjNWbGMzUmZaVzB3TjJOYTpNakF5TVMweE1DMHlOMVF3TVRvME5sbz0=",
            "Ubi-AppId": UBI_APP_ID,
            "User-Agent": "UbiServices_SDK_Unity_Light_Mobile_2018.Release.16_ANDROID64_dynamic",
            "Ubi-RequestedPlatformType": "ubimobile",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                "https://public-ubiservices.ubi.com/v1/profiles/sessions",
                json={},
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    await self.on_state_changed(
                        self.controller.serial,
                        {"state": PairingState.ERROR_CONNECTION.value},
                    )
                    raise Exception("ERROR: Couldn't get access token!")
                json_body = await resp.json()
                self.headers["Authorization"] = "Ubi_v1 " + json_body["ticket"]

    async def send_pairing_code(self):
        url = "https://prod.just-dance.com/sessions/v1/pairing-info"

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params={"code": self.pairing_code}, ssl=False) as resp:
                if resp.status != 200:
                    await self.on_state_changed(
                        self.controller.serial,
                        {"state": PairingState.ERROR_INVALID_PAIRING_CODE.value},
                    )
                    raise Exception("ERROR: Invalid pairing code!")
                json_body = await resp.json()
                self.pairing_url = json_body["pairingUrl"].replace("https://", "wss://")
                if not self.pairing_url.endswith("/"):
                    self.pairing_url += "/"
                self.pairing_url += "smartphone"
                self.tls_certificate = json_body["tlsCertificate"]
                self.requires_punch_pairing = json_body.get("requiresPunchPairing", False)

    async def send_initiate_punch_pairing(self):
        url = "https://prod.just-dance.com/sessions/v1/initiate-punch-pairing"
        json_payload = {
            "pairingCode": self.pairing_code,
            "mobileIP": self.host_ip_addr,
            "mobilePort": self.host_port,
        }

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.post(url, json=json_payload, ssl=False) as resp:
                body = await resp.text()
                if body != "OK":
                    await self.on_state_changed(
                        self.controller.serial,
                        {"state": PairingState.ERROR_PUNCH_PAIRING.value},
                    )
                    raise Exception("ERROR: Couldn't initiate punch pairing!")

    async def send_message(self, __class, data={}):
        if __class != "JD_PhoneScoringData":
            logger.debug("%s: >>> %s %s", self.controller.serial, __class, data)
        msg = {"root": {"__class": __class}}
        if data:
            msg["root"].update(data)
        try:
            if self.ws and not self.ws.closed:
                await self.ws.send(json.dumps(msg, separators=(",", ":")))
            else:
                logger.warning(
                    "%s: WebSocket is not connected or closed. Cannot send message: %s",
                    self.controller.serial,
                    __class,
                )
        except websockets.exceptions.ConnectionClosedError:
            logger.warning(
                "%s: Attempted to send message on a closed WebSocket: %s",
                self.controller.serial,
                __class,
            )
            await self.disconnect()
        except Exception:
            logger.exception("%s: Error sending message: %s", self.controller.serial, __class)
            await self.disconnect()

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
        tmp_accel_data = []
        while len(self.accel_data):
            tmp_accel_data.append(self.accel_data.pop(0))
        while len(tmp_accel_data) > 0:
            accels_num = min(len(tmp_accel_data), 10)
            await self.send_message(
                "JD_PhoneScoringData",
                {
                    "accelData": tmp_accel_data[:accels_num],
                    "timeStamp": self.number_of_accels_sent,
                },
            )
            self.number_of_accels_sent += accels_num
            tmp_accel_data = tmp_accel_data[accels_num:]

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
                await asyncio.sleep(sleep_duration)
                continue
            if not self.should_start_accelerometer:
                frames = 0
                await asyncio.sleep(sleep_duration)
                continue
            last_time = time.time()
            frames = frames + 1 if frames < 3 else 1
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
                "accelAcquisitionFreqHz": float(self.accel_acquisition_freq_hz),
                "accelAcquisitionLatency": float(self.accel_acquisition_latency),
                "accelMaxRange": float(self.accel_max_range),
            },
        )

    async def disconnect(self, should_reconnect = True):
        self.should_reconnect = should_reconnect
        if not self.is_connected:
            return
        if self.ws and not self.ws.closed:
            await self.ws.close()
        self.ws = None
        logger.debug("%s: Disconnected", self.controller.serial)
        self.is_connected = False
        await self.on_state_changed(self.controller.serial, {"state": PairingState.DISCONNECTED.value})
        if self.should_reconnect and not self.reconnection_task:
            self.reconnection_task = asyncio.create_task(self.attempt_reconnect())

    async def attempt_reconnect(self):
        retry_delay = self.reconnection_start_retry_delay
        while self.should_reconnect and not self.is_connected and not self.controller.is_connected():
            try:
                # Reconnect controller if it is not connected
                if not self.controller.is_connected():
                    is_controller_reconnected = self.controller.reconnect()
                    # We need controller to be connected to pair with the game
                    if not is_controller_reconnected:
                        continue
                # Pair with the game
                if not self.is_connected:
                    asyncio.create_task(self.pair())
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
        await self.on_state_changed(self.controller.serial, self.profile_data)

    async def on_message(self, message_str):
        message = json.loads(message_str)
        __class = message.get("__class")

        if __class != "JD_PhoneUiSetupData":
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
            await self.send_message("JD_PhoneDataCmdSync", {"phoneID": message["phoneID"]})
        elif __class == "JD_ProfilePhoneUiData":
            await self.parse_profile_data(message)
        elif __class == "JD_PlaySound_ConsoleCommandData":
            sound_index = message.get("soundIndex", 0)
            await self.controller.handle_rumble_on_sound_index(sound_index)
        elif __class == "JD_PhoneDataCmdSyncEnd":
            await self.send_message("JD_PhoneDataCmdSyncEnd", {"phoneID": message["phoneID"]})
            self.is_connected = True
            await self.on_state_changed(
                self.controller.serial, {"state": PairingState.CONNECTED.value}
            )
            await self.on_state_changed(self.controller.serial, {"state": PairingState.CONNECTED.value})
        elif __class == "JD_EnableAccelValuesSending_ConsoleCommandData":
            self.should_start_accelerometer = True
            self.number_of_accels_sent = 0
        elif __class == "JD_DisableAccelValuesSending_ConsoleCommandData":
            self.should_start_accelerometer = False
        elif __class == "InputSetup_ConsoleCommandData":
            if message.get("isEnabled", 0) == 1:
                self.is_input_allowed = True
        elif __class == "EnableCarousel_ConsoleCommandData":
            if message.get("isEnabled", 0) == 1:
                self.is_input_allowed = True
        elif __class == "JD_EnableLobbyStartbutton_ConsoleCommandData":
            if message.get("isEnabled", 0) == 1:
                self.is_input_allowed = True
        elif __class == "ShortcutSetup_ConsoleCommandData":
            if message.get("isEnabled", 0) == 1:
                self.is_input_allowed = True
        elif __class == "JD_PhoneUiShortcutData":
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
            self.available_shortcuts = shortcuts
            if hasattr(self.controller, "available_shortcuts"):
                self.controller.available_shortcuts = shortcuts
        elif __class == "JD_OpenPhoneKeyboard_ConsoleCommandData":
            self.is_search_opened = True
        elif __class == "JD_CancelKeyboard_ConsoleCommandData":
            self.is_search_opened = False
        elif __class == "JD_ClosePopup_ConsoleCommandData":
            self.is_search_opened = False
        elif __class == "JD_PhoneUiSetupData":
            self.is_input_allowed = True
            self.available_shortcuts = set()
            if message.get("setupData", {}).get("gameplaySetup", {}).get("pauseSlider", {}):
                self.available_shortcuts.add(Command.PAUSE)

            if message.get("isPopup") == 1:
                self.is_input_allowed = True
            else:
                self.is_input_allowed = message.get("inputSetup", {}).get("isEnabled", 0) == 1

        await self.on_game_message(message)

    async def send_command(self):
        while True:
            if not self.is_connected:
                await asyncio.sleep(FRAME_DURATION)
                continue
            try:
                await asyncio.sleep(FRAME_DURATION)
                if not self.is_input_allowed and not self.should_start_accelerometer:
                    continue

                if self.should_start_accelerometer:
                    cmd = self.controller.get_latest_command(commands_to_check={Command.PAUSE})
                else:
                    cmd = self.controller.get_latest_command()

                if not self.should_start_accelerometer and not cmd:
                    cmd = self.controller.get_joystick_command()

                if cmd:
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

                    if self.is_input_allowed or (
                        self.should_start_accelerometer and __class == "JD_Pause_PhoneCommandData"
                    ):
                        logger.debug("%s: >>> %s %s", self.controller.serial, __class, data)
                        await self.send_message(__class, data)
                        await asyncio.sleep(FRAME_DURATION * 30)
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
