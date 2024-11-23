import asyncio
from collections import defaultdict
import json
import random
import socket
import ssl
import time
import traceback
from enum import Enum
from urllib.parse import urlparse

import aiohttp
import websockets

from .constants import (ACCEL_ACQUISITION_FREQ_HZ, ACCEL_ACQUISITION_LATENCY,
                        ACCEL_MAX_RANGE, FRAME_DURATION, SHORTCUT_MAPPING,
                        UBI_APP_ID, UBI_SKU_ID, WS_SUBPROTOCOLS, Command,
                        JoyConButton, WsSubprotocolVersion)


class PairingState(Enum):
    IDLE = 0
    GETTING_TOKEN = 1
    PAIRING = 2
    CONNECTING = 3
    CONNECTED = 4
    DISCONNECTING = 5
    DISCONNECTED = 10

    ERROR_JOYCON = 101
    ERROR_CONNECTION = 102
    ERROR_INVALID_PAIRING_CODE = 103
    ERROR_PUNCH_PAIRING = 104
    ERROR_HOLE_PUNCHING = 105
    ERROR_CONSOLE_CONNECTION = 106


class JoyDance:
    def __init__(
            self,
            joycon,
            protocol_version,
            pairing_code=None,
            host_ip_addr=None,
            console_ip_addr=None,
            accel_acquisition_freq_hz=ACCEL_ACQUISITION_FREQ_HZ,
            accel_acquisition_latency=ACCEL_ACQUISITION_LATENCY,
            accel_max_range=ACCEL_MAX_RANGE,
            on_state_changed=None,
            on_game_message=None):
        self.joycon = joycon
        self.joycon_is_left = joycon.is_left()
        self.protocol_version = protocol_version

        if on_state_changed:
            self.on_state_changed = on_state_changed
        if on_game_message:
            self.on_game_message = on_game_message

        self.pairing_code = pairing_code
        self.host_ip_addr = host_ip_addr
        self.console_ip_addr = console_ip_addr
        self.host_port = self.get_random_port()
        self.tls_certificate = None

        self.accel_acquisition_freq_hz = accel_acquisition_freq_hz
        self.accel_acquisition_latency = accel_acquisition_latency
        self.accel_max_range = accel_max_range

        self.number_of_accels_sent = 0
        self.should_start_accelerometer = False
        self.is_input_allowed = False
        self.available_shortcuts = set()

        self.accel_data = []

        self.ws = None
        self.disconnected = False

        self.headers = {
            'Ubi-AppId': UBI_APP_ID,
            'X-SkuId': UBI_SKU_ID,
        }

        self.console_conn = None
        self.profile_data = {}
        self.v1_row_num = 0
        self.v1_col_num_per_row_id = defaultdict(int)
        self.v1_num_columns_per_row_id = {}
        self.v1_coach_id = 0
        self.v1_num_coaches = 0
        self.v1_action_id = 0
        self.is_in_lobby = False
        self.is_on_recap = False
        self.is_search_opened = False

        self.reconnection_task = None
        self.should_reconnect = True  # Flag to control reconnection attempts

    def get_random_port(self):
        ''' Randomize a port number, to be used in hole_punching() later '''
        return random.randrange(39000, 39999)

    async def on_state_changed(state):
        pass

    async def on_game_message(self, message):
        pass

    async def get_access_token(self):
        ''' Log in using a guest account, pre-defined by Ubisoft '''
        headers = {
            'Authorization': 'UbiMobile_v1 t=NTNjNWRjZGMtZjA2Yy00MTdmLWJkMjctOTNhZTcxNzU1OTkyOlcwM0N5eGZldlBTeFByK3hSa2hhQ05SMXZtdz06UjNWbGMzUmZaVzB3TjJOYTpNakF5TVMweE1DMHlOMVF3TVRvME5sbz0=',
            'Ubi-AppId': UBI_APP_ID,
            'User-Agent': 'UbiServices_SDK_Unity_Light_Mobile_2018.Release.16_ANDROID64_dynamic',
            'Ubi-RequestedPlatformType': 'ubimobile',
            'Content-Type': 'application/json',
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post('https://public-ubiservices.ubi.com/v1/profiles/sessions', json={}, ssl=False) as resp:
                if resp.status != 200:
                    await self.on_state_changed(self.joycon.serial, {'state': PairingState.ERROR_CONNECTION.value})
                    raise Exception('ERROR: Couldn\'t get access token!')

                # Add ticket to headers
                json_body = await resp.json()
                self.headers['Authorization'] = 'Ubi_v1 ' + json_body['ticket']

    async def send_pairing_code(self):
        ''' Send pairing code to JD server '''
        url = 'https://prod.just-dance.com/sessions/v1/pairing-info'

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params={'code': self.pairing_code}, ssl=False) as resp:
                if resp.status != 200:
                    await self.on_state_changed(self.joycon.serial, {'state': PairingState.ERROR_INVALID_PAIRING_CODE.value})
                    raise Exception('ERROR: Invalid pairing code!')

                json_body = await resp.json()

                self.pairing_url = json_body['pairingUrl'].replace('https://', 'wss://')
                if not self.pairing_url.endswith('/'):
                    self.pairing_url += '/'
                self.pairing_url += 'smartphone'

                self.tls_certificate = json_body['tlsCertificate']

                self.requires_punch_pairing = json_body.get('requiresPunchPairing', False)

    async def send_initiate_punch_pairing(self):
        ''' Tell console which IP address & port to connect to '''
        url = 'https://prod.just-dance.com/sessions/v1/initiate-punch-pairing'
        json_payload = {
            'pairingCode': self.pairing_code,
            'mobileIP': self.host_ip_addr,
            'mobilePort': self.host_port,
        }

        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.post(url, json=json_payload, ssl=False) as resp:
                body = await resp.text()
                if body != 'OK':
                    await self.on_state_changed(self.joycon.serial, {'state': PairingState.ERROR_PUNCH_PAIRING.value})
                    raise Exception('ERROR: Couldn\'t initiate punch pairing!')

    async def hole_punching(self):
        ''' Open a port on this machine so the console can connect to it '''
        try:
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn.settimeout(10)
            conn.bind(('0.0.0.0', self.host_port))
            conn.listen(5)

            # Accept incoming connection from console
            console_conn, addr = conn.accept()
            self.console_conn = console_conn
            print('Connected with {}:{}'.format(addr[0], addr[1]))
        except Exception as e:
            await self.on_state_changed(self.joycon.serial, {'state': PairingState.ERROR_HOLE_PUNCHING.value})
            raise e

    async def send_message(self, __class, data={}):
        ''' Send JSON message to server '''
        # if __class != 'JD_PhoneScoringData':
        #    print('>>>', __class, data)

        msg = {'root': {'__class': __class}}
        if data:
            msg['root'].update(data)

        # Remove extra spaces from JSON to reduce size

        try:
            await self.ws.send(json.dumps(msg, separators=(',', ':')))
        except Exception:
            await self.disconnect(close_ws=False)

    async def set_rumble_enabled(self, enabled):
        self.joycon.rumble_enabled = enabled
        await self.on_state_changed(self.joycon.serial, {'rumble_enabled': enabled})
        if enabled:
            # Give a little buzz to indicate that rumble is enabled
            await self.handle_rumble_on_sound_index(0)

    async def handle_rumble_on_sound_index(self, sound_index):
        """
        Handle rumble based on the sound index.
        """
        if sound_index == 0:  # Coach selection
            self.joycon.rumble(frequency=160.0, amplitude=0.3)
            await asyncio.sleep(0.15)
            self.joycon.stop_rumble()
            
        elif 1 <= sound_index <= 5:  # Stars (1-5)
            # More stars = stronger/longer vibration
            strength = 0.3 + (sound_index * 0.12)  # 0.42 to 0.90
            duration = 0.1 + (sound_index * 0.05)  # 0.15 to 0.35
            self.joycon.rumble(frequency=240.0, amplitude=strength)
            await asyncio.sleep(duration)
            self.joycon.stop_rumble()
            
        elif sound_index == 6:  # Megastar
            # Celebratory pattern: three increasing pulses
            for amp in [0.5, 0.7, 1.0]:
                self.joycon.rumble(frequency=320.0, amplitude=amp)
                await asyncio.sleep(0.15)
                self.joycon.stop_rumble()
                await asyncio.sleep(0.08)
                
        elif sound_index == 7:  # Star move
            # Quick strong pulse to highlight special move
            self.joycon.rumble(frequency=280.0, amplitude=0.8)
            await asyncio.sleep(0.2)
            self.joycon.stop_rumble()
            
        elif sound_index == 8:  # Start dance
            # Distinct "get ready" double pulse
            self.joycon.rumble(frequency=240.0, amplitude=0.6)
            await asyncio.sleep(0.15)
            self.joycon.stop_rumble()
            await asyncio.sleep(0.1)
            self.joycon.rumble(frequency=320.0, amplitude=0.8)
            await asyncio.sleep(0.2)
            self.joycon.stop_rumble()

    async def parse_profile_data(self, profile_data):
        player_id = profile_data.get('playerId')
        # Indexing starts from 0
        if player_id is not None:
            player_id += 1
        color = profile_data.get('color')
        if color is not None:
            color = [int(c * 255) for c in color]
        self.profile_data = {
            'player_name': profile_data.get('name'),
            'player_id': player_id,
            'player_color': color,
            'player_image': profile_data.get('image'),
            'skin_image': profile_data.get('skinImage'),
            'additional_message': profile_data.get('additionalMessage'),
        }

    async def on_message(self, message):
        message = json.loads(message)
        if message.get('__class') != 'JD_PhoneUiSetupData':
            # don't print UI setup data
            print('<<<', message)

        __class = message['__class']
        if __class == 'JD_PhoneDataCmdHandshakeContinue':
            await self.send_message('JD_PhoneDataCmdSync', {'phoneID': message['phoneID']})
        elif __class == 'JD_ProfilePhoneUiData':
            await self.parse_profile_data(message)
            await self.on_state_changed(self.joycon.serial, self.profile_data)
        elif __class == 'JD_PlaySound_ConsoleCommandData':
            sound_index = message.get('soundIndex', 0)
            await self.handle_rumble_on_sound_index(sound_index)
        elif __class == 'JD_PhoneDataCmdSyncEnd':
            await self.send_message('JD_PhoneDataCmdSyncEnd', {'phoneID': message['phoneID']})
            await self.on_state_changed(self.joycon.serial, {'state': PairingState.CONNECTED.value})
        elif __class == 'JD_EnableAccelValuesSending_ConsoleCommandData':
            self.should_start_accelerometer = True
            self.number_of_accels_sent = 0
        elif __class == 'JD_DisableAccelValuesSending_ConsoleCommandData':
            self.should_start_accelerometer = False
        elif __class == 'InputSetup_ConsoleCommandData':
            if message.get('isEnabled', 0) == 1:
                self.is_input_allowed = True

            if self.protocol_version == WsSubprotocolVersion.V1:
                await self.parse_carousel_position_setup_data(message)
        elif __class == 'EnableCarousel_ConsoleCommandData':
            if message.get('isEnabled', 0) == 1:
                self.is_input_allowed = True
        elif __class == 'JD_EnableLobbyStartbutton_ConsoleCommandData':
            if message.get('isEnabled', 0) == 1:
                self.is_input_allowed = True
        elif __class == 'ShortcutSetup_ConsoleCommandData':
            if message.get('isEnabled', 0) == 1:
                self.is_input_allowed = True
        elif __class == 'JD_PhoneUiShortcutData':
            shortcuts = set()
            for item in message.get('shortcuts', []):
                if item['__class'] == 'JD_PhoneAction_Shortcut':
                    try:
                        shortcuts.add(Command(item['shortcutType']))
                    except Exception as e:
                        print('Unknown Command: ', e)
            self.available_shortcuts = shortcuts
        elif __class == 'JD_OpenPhoneKeyboard_ConsoleCommandData':
            self.is_search_opened = True
        elif __class == 'JD_CancelKeyboard_ConsoleCommandData':
            self.is_search_opened = False
        elif __class == 'JD_ClosePopup_ConsoleCommandData':
            self.is_search_opened = False
        elif __class == 'JD_PhoneUiSetupData':
            self.is_input_allowed = True
            self.available_shortcuts = set()
            if message.get('setupData', {}).get('gameplaySetup', {}).get('pauseSlider', {}):
                self.available_shortcuts.add(Command.PAUSE)

            if self.protocol_version == WsSubprotocolVersion.V1:
                await self.parse_phone_setup_data(message)

            if message['isPopup'] == 1:
                self.is_input_allowed = True
            else:
                self.is_input_allowed = message.get('inputSetup', {}).get('isEnabled', 0) == 1

        await self.on_game_message(message)

    async def send_hello(self):
        print('Pairing...')

        await self.send_message('JD_PhoneDataCmdHandshakeHello', {
            'accelAcquisitionFreqHz': float(self.accel_acquisition_freq_hz),
            'accelAcquisitionLatency': float(self.accel_acquisition_latency),
            'accelMaxRange': float(self.accel_max_range),
        })

        async for message in self.ws:
            await self.on_message(message)

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
            if self.disconnected:
                break

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
            sleep_duration = FRAME_DURATION - (dt - sleep_duration)

    async def collect_accelerometer_data(self):
        if self.disconnected:
            return

        if not self.should_start_accelerometer:
            self.accel_data = []
            return

        try:
            accels = self.joycon.get_accels()  # ([x, y, z],...)
            self.accel_data += accels
        except OSError:
            self.disconnect()
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

            await self.send_message('JD_PhoneScoringData', {
                'accelData': tmp_accel_data[:accels_num],
                'timeStamp': self.number_of_accels_sent,
            })

            self.number_of_accels_sent += accels_num
            tmp_accel_data = tmp_accel_data[accels_num:]

    async def preprocess_command_for_v2(self, cmd):
        data = {}
        if cmd == Command.PAUSE:
            __class = 'JD_Pause_PhoneCommandData'
        elif type(cmd.value) == str:
            __class = 'JD_Custom_PhoneCommandData'
            data['identifier'] = cmd.value
        else:
            __class = 'JD_Input_PhoneCommandData'
            data['input'] = cmd.value
        return __class, data

    async def preprocess_command_for_v1(self, cmd):
        data = {}
        if cmd == Command.PAUSE:
            __class = 'JD_Pause_PhoneCommandData'
        elif cmd == Command.BACK:
            if self.is_search_opened:
                __class = 'JD_CancelKeyboard_PhoneCommandData'
            else:
                __class = 'JD_Custom_PhoneCommandData'
                data['identifier'] = cmd.value
        elif type(cmd.value) == str:
            __class = 'JD_Custom_PhoneCommandData'
            data['identifier'] = cmd.value
        elif cmd == Command.V1_FAVORITE:
            __class = 'JD_Input_PhoneCommandData'
            data['input'] = cmd.value
        elif cmd == Command.ACCEPT:
            if self.is_in_lobby:
                __class = 'JD_StartGame_PhoneCommandData'
            elif self.is_on_recap:
                __class = 'JD_Input_PhoneCommandData'
                data['input'] = Command.ACCEPT.value
            elif self.is_search_opened:
                __class = 'JD_Input_PhoneCommandData'
                data['input'] = Command.V1_KEYBOARD_ERROR_OK.value
            else:
                __class = 'ValidateAction_PhoneCommandData'
                data['rowIndex'] = self.v1_row_num
                data['itemIndex'] = self.v1_col_num_per_row_id[self.v1_row_num]
                data['actionIndex'] = self.v1_action_id
        # further commands are enabled only then is_input_allowed=True
        elif not self.is_input_allowed:
            return None, None
        elif cmd == Command.UP:
            if self.is_in_lobby:
                return None, None
            __class = 'ChangeRow_PhoneCommandData'
            if self.v1_row_num <= 0:
                self.v1_row_num = len(self.v1_num_columns_per_row_id)
                return None, None
            self.v1_row_num -= 1
            data['rowIndex'] = self.v1_row_num
        elif cmd == Command.DOWN:
            if self.is_in_lobby:
                return None, None
            __class = 'ChangeRow_PhoneCommandData'
            if self.v1_row_num >= len(self.v1_num_columns_per_row_id) - 1:
                self.v1_row_num = 0
                return None, None
            self.v1_row_num += 1
            data['rowIndex'] = self.v1_row_num
        elif cmd == Command.LEFT:
            if not self.is_in_lobby:
                __class = 'ChangeItem_PhoneCommandData'
                data['rowIndex'] = self.v1_row_num
                self.v1_col_num_per_row_id[self.v1_row_num] -= 1
                if self.v1_col_num_per_row_id[self.v1_row_num] < 0:
                    self.v1_col_num_per_row_id[self.v1_row_num] = self.v1_num_columns_per_row_id[self.v1_row_num] - 1
                data['itemIndex'] = self.v1_col_num_per_row_id[self.v1_row_num]
            else:
                __class = 'JD_ChangeCoach_PhoneCommandData'
                if self.v1_coach_id == 0:
                    return None, None
                elif self.v1_coach_id < 0:
                    self.v1_coach_id = 0
                    return None, None
                self.v1_coach_id -= 1
                data['coachId'] = self.v1_coach_id
        elif cmd == Command.RIGHT:
            if not self.is_in_lobby:
                __class = 'ChangeItem_PhoneCommandData'
                data['rowIndex'] = self.v1_row_num
                self.v1_col_num_per_row_id[self.v1_row_num] += 1
                if self.v1_col_num_per_row_id[self.v1_row_num] >= self.v1_num_columns_per_row_id[self.v1_row_num]:
                    self.v1_col_num_per_row_id[self.v1_row_num] = 0
                data['itemIndex'] = self.v1_col_num_per_row_id[self.v1_row_num]
            else:
                __class = 'JD_ChangeCoach_PhoneCommandData'
                if self.v1_coach_id >= self.v1_num_coaches - 1:
                    self.v1_coach_id = self.v1_num_coaches - 1
                    return None, None
                self.v1_coach_id += 1
                data['coachId'] = self.v1_coach_id
        return __class, data

    async def preprocess_command(self, cmd):
        if self.protocol_version == WsSubprotocolVersion.V1:
            return await self.preprocess_command_for_v1(cmd)
        else:
            return await self.preprocess_command_for_v2(cmd)
        return None, None

    async def send_command(self):
        ''' Capture Joycon's input and send to console.'''
        while True:
            if self.disconnected:
                return

            try:
                await asyncio.sleep(FRAME_DURATION)
                if not self.is_input_allowed and not self.should_start_accelerometer:
                    continue

                cmd = None
                # Get pressed button
                for event_type, status in self.joycon.events():
                    if status == 0:  # 0 = pressed, 1 = released
                        continue

                    joycon_button = JoyConButton(event_type)
                    if self.should_start_accelerometer:  # Only allow to send Pause command while playing
                        if joycon_button == JoyConButton.PLUS or joycon_button == JoyConButton.MINUS:
                            cmd = Command.PAUSE
                    else:
                        if joycon_button == JoyConButton.A or joycon_button == JoyConButton.RIGHT:
                            cmd = Command.ACCEPT
                        elif joycon_button == JoyConButton.B or joycon_button == JoyConButton.DOWN:
                            cmd = Command.BACK
                        elif joycon_button in SHORTCUT_MAPPING:
                            # Get command depends on which button is being pressed & which shortcuts are available
                            for shortcut in SHORTCUT_MAPPING[joycon_button]:
                                if shortcut in self.available_shortcuts:
                                    cmd = shortcut
                                    break

                # Get joystick direction
                if not self.should_start_accelerometer and not cmd:
                    status = self.joycon.get_status()

                    # Check which Joycon (L/R) is being used
                    stick = status['analog-sticks']['left'] if self.joycon_is_left else status['analog-sticks']['right']
                    vertical = stick['vertical']
                    horizontal = stick['horizontal']

                    if vertical < -0.5:
                        cmd = Command.DOWN
                    elif vertical > 0.5:
                        cmd = Command.UP
                    elif horizontal < -0.5:
                        cmd = Command.LEFT
                    elif horizontal > 0.5:
                        cmd = Command.RIGHT

                # Send command to server
                if cmd:
                    try:
                        __class, data = await self.preprocess_command(cmd)
                    except Exception:
                        print(f"An error occurred while processing command for {self.joycon.serial}.")
                        print(traceback.format_exc())
                        continue
                    # if __class is None, it means the command is not allowed to be sent
                    if __class is None:
                        continue

                    # Only send input when it's allowed to, otherwise we might get a disconnection
                    if self.is_input_allowed:
                        await self.send_message(__class, data)
                        await asyncio.sleep(FRAME_DURATION * 10)
            except Exception:
                traceback.print_exc()
                await self.disconnect()

    async def connect_ws(self):
        server_hostname = None
        if self.protocol_version == WsSubprotocolVersion.V1:
            ssl_context = None
        else:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.set_ciphers('ALL')
            ssl_context.options &= ~ssl.OP_NO_SSLv3
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            if self.tls_certificate:
                ssl_context.load_verify_locations(cadata=self.tls_certificate)

            if self.pairing_url.startswith('192.168.') or self.pairing_url.startswith('10.'):
                if self.console_conn:
                    server_hostname = self.console_conn.getpeername()[0]
            else:
                # Stadia
                tmp = urlparse(self.pairing_url)
                server_hostname = tmp.hostname

        subprotocol = WS_SUBPROTOCOLS[self.protocol_version.value]
        try:
            async with websockets.connect(
                    self.pairing_url,
                    subprotocols=[subprotocol],
                    sock=self.console_conn,
                    ssl=ssl_context,
                    ping_timeout=None,
                    server_hostname=server_hostname
            ) as websocket:
                try:
                    self.ws = websocket

                    await asyncio.gather(
                        self.send_hello(),
                        self.tick(),
                        self.send_command(),
                    )

                except websockets.ConnectionClosed:
                    await self.on_state_changed(self.joycon.serial, {'state': PairingState.ERROR_CONSOLE_CONNECTION.value})
                    await self.disconnect(close_ws=False)
        except Exception:
            traceback.print_exc()
            await self.on_state_changed(self.joycon.serial, {'state': PairingState.ERROR_CONSOLE_CONNECTION.value})
            await self.disconnect(close_ws=False)

    async def disconnect(self, close_ws=True):
        print('disconnected')
        self.disconnected = True
        await self.on_state_changed(self.joycon.serial, {'state': PairingState.DISCONNECTED.value})
        # Don't fully delete the joycon, just close it
        self.joycon.close()

        if close_ws and self.ws:
            await self.ws.close()

        # Start reconnection attempt if enabled
        if self.should_reconnect and not self.reconnection_task:
            self.reconnection_task = asyncio.create_task(self.attempt_reconnect())

    async def attempt_reconnect(self):
        """Attempts to reconnect to the JoyCon periodically"""
        retry_delay = 1  # Start with 1 second delay
        max_delay = 30   # Maximum delay between attempts
        
        while self.should_reconnect:
            try:
                # Attempt to reconnect the joycon
                await self.joycon.reconnect()
                
                if self.joycon.is_connected():
                    print("JoyCon reconnected successfully")
                    self.disconnected = False
                    self.reconnection_task = None
                    
                    # Restart the main connection flow
                    await self.on_state_changed(self.joycon.serial, {'state': PairingState.IDLE.value})
                    asyncio.create_task(self.pair())
                    return
                
            except Exception as e:
                print(f"Reconnection attempt failed: {e}")
                
            # Exponential backoff with maximum delay
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)

    async def stop_reconnection(self):
        """Stops the reconnection attempts"""
        self.should_reconnect = False
        if self.reconnection_task:
            self.reconnection_task.cancel()
            self.reconnection_task = None

    async def pair(self):
        try:
            if self.console_ip_addr:
                await self.on_state_changed(self.joycon.serial, {'state': PairingState.CONNECTING.value})
                if self.protocol_version == WsSubprotocolVersion.V1:
                    self.pairing_url = 'ws://{}:8080/smartphone'.format(self.console_ip_addr)
                else:
                    self.pairing_url = 'wss://{}:8080/smartphone'.format(self.console_ip_addr)
            else:
                await self.on_state_changed(self.joycon.serial, {'state': PairingState.GETTING_TOKEN.value})
                print('Getting authorication token...')
                await self.get_access_token()

                await self.on_state_changed(self.joycon.serial, {'state': PairingState.PAIRING.value})
                print('Sending pairing code...')
                await self.send_pairing_code()

                await self.on_state_changed(self.joycon.serial, {'state': PairingState.CONNECTING.value})
                print('Connecting with console...')
                if self.requires_punch_pairing:
                    await self.send_initiate_punch_pairing()
                    await self.hole_punching()

            await self.connect_ws()
        except Exception:
            await self.disconnect()
            traceback.print_exc()

    async def parse_carousel_position_setup_data(self, message):
        """
        Parse the carousel position setup data and update the state accordingly.
        """
        # carouselPosSetup can be in inputSetup or at the root level
        carousel_pos_setup = None
        if 'carouselPosSetup' in message:
            carousel_pos_setup = message['carouselPosSetup']
        elif 'inputSetup' in message:
            carousel_pos_setup = message['inputSetup'].get('carouselPosSetup', {})

        if carousel_pos_setup:
            self.v1_row_num = carousel_pos_setup['rowIndex']
            self.v1_col_num_per_row_id[self.v1_row_num] = carousel_pos_setup['itemIndex']
            self.v1_action_id = carousel_pos_setup['actionIndex']

    async def parse_phone_setup_data(self, data):
        """
        Parse the phone setup data and update the state accordingly.
        Only needed for protocol v1, as it needs to know the current setup of input carousel.
        """
        # Reset the state
        self.is_on_recap = False
        self.is_in_lobby = False
        self.is_search_opened = False
        # Extract the main carousel structure
        man_carousel_rows = data.get('setupData', {}).get('mainCarousel', {}).get('rows', [])
        if man_carousel_rows:
            # Extract the main carousel rows
            num_columns_per_row_id = {}
            for row_num, row in enumerate(man_carousel_rows):
                num_columns_per_row_id[row_num] = len(row['items'])
            self.v1_num_columns_per_row_id = num_columns_per_row_id
        # Check if the game is on pop up screen
        carousel_pop_setup = data.get('inputSetup', {}).get('carouselPosSetup', {})
        if carousel_pop_setup:
            self.v1_row_num = carousel_pop_setup['rowIndex']
            self.v1_col_num_per_row_id[self.v1_row_num] = carousel_pop_setup['itemIndex']
            self.v1_action_id = carousel_pop_setup['actionIndex']
            self.v1_coach_id = 0
        # Check if the game is in lobby screen, and extract the number of coaches
        coaches = data.get('setupData', {}).get('lobbySetup', {}).get('coaches', [])
        if coaches:
            self.is_in_lobby = True
            self.v1_num_coaches = len(coaches)
        # Check if the game is on recap screen
        if data.get('setupData', {}).get('recapSetup', {}):
            self.is_on_recap = True
        # Extract available shortcuts
        shortcuts = data.get('setupData', {}).get('shortcuts', [])
        if shortcuts:
            shortcuts_identifiers = set()
            for shortcut in data['setupData']['shortcuts']:
                try:
                    shortcuts_identifiers.add(Command(json.loads(shortcut['command'])['root']['identifier']))
                except Exception:
                    try:
                        shortcuts_identifiers.add(Command(json.loads(shortcut['command'])['root']['input']))
                    except Exception:
                        print('Error adding shortcut', shortcut)
            self.available_shortcuts = shortcuts_identifiers
