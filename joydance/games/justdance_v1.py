from .justdance_abstract import JustDanceGameAbstract
from collections import defaultdict
import asyncio
import json
import logging
from urllib.parse import urlparse
import websockets
from joydance.constants import (
    WS_SUBPROTOCOLS,
    Command,
    PairingState,
    WsSubprotocolVersion,
)

logger = logging.getLogger("joydance")


class JustDanceGameV1(JustDanceGameAbstract):
    """
    Game protocol handler for Just Dance protocol version v1.
    Handles all v1-specific communication, message parsing, and command preprocessing.
    """

    def __init__(self, controller, *args, **kwargs):
        super().__init__(controller, *args, **kwargs)
        self.is_main_player = True
        self._carousel_setup = defaultdict(
            lambda: {
                "row_num": 0,
                "col_num_per_row_id": defaultdict(int),
                "num_columns_per_row_id": {},
                "item_actions": {},
                "action_id": 0,
            }
        )
        self.current_carousel_type = "main"
        self.coach_id = 0
        self.num_coaches = 0
        self.is_in_lobby = False
        self.is_on_recap = False

    @property
    def carousel_setup(self):
        return self._carousel_setup[self.current_carousel_type]

    @property
    def row_num(self):
        return self._carousel_setup[self.current_carousel_type]["row_num"]

    @row_num.setter
    def row_num(self, value):
        self._carousel_setup[self.current_carousel_type]["row_num"] = value

    @property
    def col_num_per_row_id(self):
        return self._carousel_setup[self.current_carousel_type]["col_num_per_row_id"]

    @col_num_per_row_id.setter
    def col_num_per_row_id(self, value):
        self._carousel_setup[self.current_carousel_type]["col_num_per_row_id"] = value

    @property
    def num_columns_per_row_id(self):
        return self._carousel_setup[self.current_carousel_type]["num_columns_per_row_id"]

    @num_columns_per_row_id.setter
    def num_columns_per_row_id(self, value):
        self._carousel_setup[self.current_carousel_type]["num_columns_per_row_id"] = value

    @property
    def item_actions(self):
        return self._carousel_setup[self.current_carousel_type]["item_actions"]

    @item_actions.setter
    def item_actions(self, value):
        self._carousel_setup[self.current_carousel_type]["item_actions"] = value

    @property
    def action_id(self):
        return self._carousel_setup[self.current_carousel_type]["action_id"]

    @action_id.setter
    def action_id(self, value):
        self._carousel_setup[self.current_carousel_type]["action_id"] = value

    async def on_message(self, message_str):
        # Handle general messages first
        await super().on_message(message_str)
        message_dict = json.loads(message_str)
        __class = message_dict.get("__class")
        if __class == "JD_ProfilePhoneUiData":
            player_id = self.profile_data.get("player_id")
            if player_id is not None:
                self.is_main_player = player_id == 1

        elif __class == "InputSetup_ConsoleCommandData":
            await self.parse_carousel_position_setup_data(message_dict)

        elif __class == "JD_PhoneUiSetupData":
            await self.parse_phone_setup_data(message_dict)

    async def parse_carousel_position_setup_data(self, message):
        carousel_pos_setup = None
        if "carouselPosSetup" in message:
            carousel_pos_setup = message["carouselPosSetup"]
        elif "inputSetup" in message:
            carousel_pos_setup = message["inputSetup"].get("carouselPosSetup", {})
        if carousel_pos_setup:
            self.row_num = carousel_pos_setup["rowIndex"]
            self.col_num_per_row_id[self.row_num] = carousel_pos_setup["itemIndex"]
            self.action_id = carousel_pos_setup["actionIndex"]

    @staticmethod
    def parse_actions(raw_item_actions):
        item_actions = []
        for item_action in raw_item_actions:
            parsed_action = item_action.get("command", "")
            if parsed_action:
                try:
                    parsed_action = json.loads(parsed_action)["root"]
                except Exception:
                    logger.debug("V1 Failed to parse command: %s", item_action)
                    parsed_action = ""
            logger.debug("V1 parse_actions: %s, %s", item_action, parsed_action)
            item_actions.append(parsed_action)
        return item_actions

    async def parse_phone_setup_data(self, data):
        self.is_on_recap = False
        self.is_in_lobby = False

        if (
            data.get("isPopup")
            or data.get("setupData", {})
            .get("mainCarousel", {})
            .get("rows", [{}])[0]
            .get("items", [{}])[0]
            .get("title")
            == "[icon:GEN-VALIDATE] Quit"
        ):
            self.current_carousel_type = "popup"
        elif data.get("setupData", {}).get("lobbySetup"):
            self.current_carousel_type = "lobby"
        elif data.get("setupData", {}).get("recapSetup"):
            self.current_carousel_type = "recap"
        else:
            self.current_carousel_type = "main"

        main_carousel_rows = data.get("setupData", {}).get("mainCarousel", {}).get("rows", [])
        if main_carousel_rows:
            num_columns_per_row_id = {}
            all_item_actions = defaultdict(dict)
            for row_num, row in enumerate(main_carousel_rows):
                num_columns_per_row_id[row_num] = len(row["items"])
                for item_idx, item in enumerate(row.get("items", [])):
                    all_item_actions[row_num][item_idx] = self.parse_actions(item.get("actions", []))
            self.item_actions = all_item_actions
            self.num_columns_per_row_id = num_columns_per_row_id

        carousel_pos_setup = data.get("inputSetup", {}).get("carouselPosSetup", {})
        if carousel_pos_setup:
            self.row_num = carousel_pos_setup["rowIndex"]
            self.col_num_per_row_id[self.row_num] = carousel_pos_setup["itemIndex"]
            self.action_id = carousel_pos_setup["actionIndex"]
            self.coach_id = 0

        lobby_setup = data.get("setupData", {}).get("lobbySetup", {})
        if lobby_setup:
            self.is_in_lobby = True
            self.num_columns_per_row_id = {0: 1}
            self.item_actions = {
                0: {0: self.parse_actions(lobby_setup.get("startButton", {}).get("actions", []))}
            }
            self.num_coaches = len(lobby_setup.get("coaches", []))

        if data.get("setupData", {}).get("recapSetup", {}):
            self.is_on_recap = True

        shortcuts_data = data.get("setupData", {}).get("shortcuts", [])
        if shortcuts_data:
            shortcuts_identifiers = set()
            for shortcut in shortcuts_data:
                try:
                    command_root = json.loads(shortcut["command"])["root"]
                    if "identifier" in command_root:
                        shortcuts_identifiers.add(Command(command_root["identifier"]))
                    elif "input" in command_root:
                        shortcuts_identifiers.add(Command(command_root["input"]))
                except Exception:
                    logger.exception("V1: Failed to parse shortcut: %s", shortcut)
            if shortcuts_identifiers:
                self.available_shortcuts = shortcuts_identifiers
                if hasattr(self.controller, "available_shortcuts"):
                    self.controller.available_shortcuts = shortcuts_identifiers

    async def connect_ws(self):
        server_hostname = None
        ssl_context = None

        if self.pairing_url.startswith("ws://192.168.") or self.pairing_url.startswith("ws://10."):
            if self.console_conn:
                server_hostname = self.console_conn.getpeername()[0]
        elif not self.pairing_url.startswith("ws://"):
            logger.warning(
                f"{self.controller.serial}: V1 connect_ws expects ws:// pairing_url, got {self.pairing_url}"
            )
            tmp = urlparse(self.pairing_url)
            server_hostname = tmp.hostname

        subprotocol = WS_SUBPROTOCOLS[WsSubprotocolVersion.V1.value]
        try:
            logger.debug(
                f"{self.controller.serial}: V1 connecting to {self.pairing_url} with subprotocol {subprotocol}, sock: {'present' if self.console_conn else 'None'}, server_hostname: {server_hostname}"
            )
            async with websockets.connect(
                self.pairing_url,
                subprotocols=[subprotocol],
                sock=self.console_conn,
                ssl=ssl_context,
                ping_timeout=None,
                server_hostname=(
                    server_hostname
                    if server_hostname
                    else (
                        urlparse(self.pairing_url).hostname
                        if not (
                            self.pairing_url.startswith("ws://192.168.")
                            or self.pairing_url.startswith("ws://10.")
                        )
                        else None
                    )
                ),
            ) as websocket:
                self.ws = websocket
                self.disconnected = False
                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.CONNECTED.value}
                )

                receive_task = asyncio.create_task(self._message_receive_loop())

                await asyncio.gather(self.send_hello(), self.tick(), self.send_command())

        except websockets.ConnectionClosed as e:
            logger.warning(f"{self.controller.serial}: V1 WebSocket connection closed: {e}")
            await self.on_state_changed(
                self.controller.serial,
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect(close_ws=False)
        except ConnectionRefusedError as e:
            logger.error(f"{self.controller.serial}: V1 Connection refused for {self.pairing_url}: {e}")
            await self.on_state_changed(
                self.controller.serial,
                {"state": PairingState.ERROR_CONNECTION.value, "error_details": str(e)},
            )
            await self.disconnect(close_ws=False)
        except Exception as e:
            logger.exception(
                "%s: V1 An error occurred while connecting WebSocket to console.",
                self.controller.serial,
            )
            await self.on_state_changed(
                self.controller.serial,
                {
                    "state": PairingState.ERROR_CONSOLE_CONNECTION.value,
                    "error_details": str(e),
                },
            )
            await self.disconnect(close_ws=True)
        finally:
            if "receive_task" in locals() and not receive_task.done():
                receive_task.cancel()

    async def _message_receive_loop(self):
        try:
            async for message_str in self.ws:
                await self.on_message(message_str)
        except websockets.ConnectionClosed:
            logger.info(
                "%s: V1 WebSocket connection closed while receiving messages.",
                self.controller.serial,
            )
        except Exception as e:
            logger.exception("%s: V1 Error in message receive loop.", self.controller.serial)
            await self.disconnect(close_ws=True)

    async def pair(self):
        try:
            self.disconnected = False
            self.should_reconnect = True

            if self.console_ip_addr:
                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.CONNECTING.value}
                )
                self.pairing_url = f"ws://{self.console_ip_addr}:8080/smartphone"

            else:
                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.GETTING_TOKEN.value}
                )
                logger.debug("%s: Getting V1 authorization token...", self.controller.serial)
                await self.get_access_token()

                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.PAIRING.value}
                )
                logger.debug("%s: V1 Sending pairing code...", self.controller.serial)
                await self.send_pairing_code()

                if self.pairing_url.startswith("wss://"):
                    self.pairing_url = self.pairing_url.replace("wss://", "ws://", 1)
                    logger.debug(
                        "%s: V1 Adjusted pairing URL to ws: %s",
                        self.controller.serial,
                        self.pairing_url,
                    )

                await self.on_state_changed(
                    self.controller.serial, {"state": PairingState.CONNECTING.value}
                )
                logger.debug("%s: V1 Connecting with console...", self.controller.serial)
                if self.requires_punch_pairing:
                    await self.send_initiate_punch_pairing()

            await self.connect_ws()
        except Exception:
            await self.disconnect()
            logger.exception("%s: V1 An error occurred while pairing.", self.controller.serial)

    async def preprocess_command(self, cmd):
        __class, data = None, {}

        if cmd == Command.PAUSE:
            __class = "JD_Pause_PhoneCommandData"
        # further commands are only for main player
        elif not self.is_main_player:
            # TODO: code repetition, need to refactor
            # non-main controller can only change coach in lobby
            if not self.is_in_lobby:
                return None, None
            if cmd == Command.LEFT:
                __class = "JD_ChangeCoach_PhoneCommandData"
                if self.coach_id == 0:
                    return None, None
                elif self.coach_id < 0:
                    self.coach_id = 0
                    return None, None
                self.coach_id -= 1
                data["coachId"] = self.coach_id
            elif cmd == Command.RIGHT:
                __class = "JD_ChangeCoach_PhoneCommandData"
                if self.coach_id >= self.num_coaches - 1:
                    self.coach_id = self.num_coaches - 1
                    return None, None
                self.coach_id += 1
                data["coachId"] = self.coach_id
            return __class, data
        elif cmd == Command.BACK:
            if self.is_search_opened:
                __class = "JD_CancelKeyboard_PhoneCommandData"
            else:
                __class = "JD_Custom_PhoneCommandData"
                data["identifier"] = cmd.value
        elif type(cmd.value) == str:
            __class = "JD_Custom_PhoneCommandData"
            data["identifier"] = cmd.value
        elif cmd == Command.V1_FAVORITE:
            __class = "JD_Input_PhoneCommandData"
            data["input"] = cmd.value
        elif cmd == Command.ACCEPT:
            row_idx, col_idx, action_idx = (
                self.row_num,
                self.col_num_per_row_id[self.row_num],
                self.action_id,
            )
            try:
                selected_action = self.item_actions[row_idx][col_idx][action_idx].copy()
            except:
                logger.error(
                    "Failed to get selected action %s, %s, %s, %s",
                    row_idx,
                    col_idx,
                    action_idx,
                    self.item_actions,
                )
                selected_action = ""
            if selected_action:
                return selected_action.pop("__class", None), selected_action
            else:
                __class = "ValidateAction_PhoneCommandData"
                data["rowIndex"] = row_idx
                data["itemIndex"] = col_idx
                data["actionIndex"] = action_idx
            # TODO: delete after testing updated logic
            # if self.is_in_lobby:
            #     __class = 'JD_StartGame_PhoneCommandData'
            # elif self.is_on_recap:
            #     __class = 'JD_Input_PhoneCommandData'
            #     data['input'] = Command.ACCEPT.value
            # elif self.is_search_opened:
            #     __class = 'JD_Input_PhoneCommandData'
            #     data['input'] = Command.V1_KEYBOARD_ERROR_OK.value
            # else:
            #     __class = 'ValidateAction_PhoneCommandData'
            #     data['rowIndex'] = self.row_num
            #     data['itemIndex'] = self.col_num_per_row_id[self.row_num]
            #     data['actionIndex'] = self.action_id
        # further commands are enabled only then is_input_allowed=True
        elif not self.is_input_allowed:
            return None, None
        elif cmd == Command.UP:
            if self.is_in_lobby:
                return None, None
            __class = "ChangeRow_PhoneCommandData"
            self.row_num -= 1
            if self.row_num < 0:
                self.row_num = len(self.num_columns_per_row_id) - 1
            data["rowIndex"] = self.row_num
        elif cmd == Command.DOWN:
            if self.is_in_lobby:
                return None, None
            __class = "ChangeRow_PhoneCommandData"
            self.row_num += 1
            if self.row_num >= len(self.num_columns_per_row_id):
                self.row_num = 0
            data["rowIndex"] = self.row_num
        elif cmd == Command.LEFT:
            if not self.is_in_lobby:
                __class = "ChangeItem_PhoneCommandData"
                data["rowIndex"] = self.row_num
                self.col_num_per_row_id[self.row_num] -= 1
                if self.col_num_per_row_id[self.row_num] < 0:
                    self.col_num_per_row_id[self.row_num] = self.num_columns_per_row_id[self.row_num] - 1
                data["itemIndex"] = self.col_num_per_row_id[self.row_num]
            else:
                __class = "JD_ChangeCoach_PhoneCommandData"
                if self.coach_id == 0:
                    return None, None
                elif self.coach_id < 0:
                    self.coach_id = 0
                    return None, None
                self.coach_id -= 1
                data["coachId"] = self.coach_id
        elif cmd == Command.RIGHT:
            if not self.is_in_lobby:
                __class = "ChangeItem_PhoneCommandData"
                data["rowIndex"] = self.row_num
                self.col_num_per_row_id[self.row_num] += 1
                if self.col_num_per_row_id[self.row_num] >= self.num_columns_per_row_id[self.row_num]:
                    self.col_num_per_row_id[self.row_num] = 0
                data["itemIndex"] = self.col_num_per_row_id[self.row_num]
            else:
                __class = "JD_ChangeCoach_PhoneCommandData"
                if self.coach_id >= self.num_coaches - 1:
                    self.coach_id = self.num_coaches - 1
                    return None, None
                self.coach_id += 1
                data["coachId"] = self.coach_id
        return __class, data
