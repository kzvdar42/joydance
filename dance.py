import asyncio
import json
import logging
import platform
import time
import os
import sys
import mimetypes

import aiohttp
import hid
from aiohttp import WSMsgType, web

from joydance import JoyDance, PairingState
from joydance.config_handler import ConfigHandler, get_datadir
from joydance.constants import (
    WsCommand, PairingMethod, JOYDANCE_VERSION, WsSubprotocolVersion
)
from pycon import ButtonEventJoyCon, JoyCon
from pycon.constants import JOYCON_PRODUCT_IDS, JOYCON_VENDOR_ID

logging.getLogger('asyncio').setLevel(logging.WARNING)


CONFIG_PATHS = [
    'config.cfg',
    os.path.join(get_datadir(), 'config.cfg')
]


async def get_device_ids():
    devices = hid.enumerate(JOYCON_VENDOR_ID, 0)

    out = []
    for device in devices:
        vendor_id = device['vendor_id']
        product_id = device['product_id']
        product_string = device['product_string']
        serial = device.get('serial') or device.get('serial_number')

        if product_id not in JOYCON_PRODUCT_IDS:
            continue

        if not product_string:
            continue

        out.append({
            'vendor_id': vendor_id,
            'product_id': product_id,
            'serial': serial,
            'product_string': product_string,
        })

    return out


async def get_joycon_list(app):
    joycons = []
    devices = await get_device_ids()

    for dev in devices:
        if dev['serial'] in app['joycons_info']:
            info = app['joycons_info'][dev['serial']]
        else:
            joycon = JoyCon(dev['vendor_id'], dev['product_id'], dev['serial'])
            # Wait for initial data
            for _ in range(3):
                time.sleep(0.05)
                battery_level = joycon.get_battery_level()
                if battery_level > 0:
                    break

            color = '#%02x%02x%02x' % joycon.color_body

            # Temporary fix for Windows
            if platform.system() != 'Windows':
                joycon.__del__()

            info = {
                'vendor_id': dev['vendor_id'],
                'product_id': dev['product_id'],
                'serial': dev['serial'],
                'name': dev['product_string'],
                'color': color,
                'battery_level': battery_level,
                'is_left': joycon.is_left(),
                'state': PairingState.IDLE.value,
                'pairing_code': '',
                'rumble_enabled': joycon.rumble_enabled,
            }

            app['joycons_info'][dev['serial']] = info

        joycons.append(info)

    return sorted(joycons, key=lambda x: (x['name'], x['color'], x['serial']))


async def connect_joycon(app, ws, data):
    async def on_joydance_state_changed(serial, update_dict):
        app['joycons_info'][serial].update(update_dict)
        try:
            await ws_send_response(ws, WsCommand.UPDATE_JOYCON_STATE, app['joycons_info'][serial])
        except Exception as e:
            print(e)

    async def on_game_message(message):
        __class = message.get('__class')
        if __class == 'JD_OpenPhoneKeyboard_ConsoleCommandData':
            await ws_send_response(ws, WsCommand.SHOW_SEARCH, {'serial': serial})
        elif __class == 'JD_CancelKeyboard_ConsoleCommandData':
            await ws_send_response(ws, WsCommand.HIDE_SEARCH, {'serial': serial})

    print(data)

    serial = data['joycon_serial']
    product_id = app['joycons_info'][serial]['product_id']
    vendor_id = app['joycons_info'][serial]['vendor_id']

    pairing_method = data['pairing_method']
    host_ip_addr = data['host_ip_addr']
    console_ip_addr = data['console_ip_addr']
    pairing_code = data['pairing_code']

    if not app['config_handler'].is_new_config_valid(data):
        return

    config = app['config_handler'].data.copy()
    config['pairing_code'] = pairing_code
    config['pairing_method'] = pairing_method
    config['host_ip_addr'] = host_ip_addr
    config['console_ip_addr'] = console_ip_addr
    app['config_handler'].data = config

    if pairing_method == PairingMethod.DEFAULT.value or pairing_method == PairingMethod.STADIA.value:
        app['joycons_info'][serial]['pairing_code'] = pairing_code
        console_ip_addr = None
    else:
        app['joycons_info'][serial]['pairing_code'] = ''

    joycon = ButtonEventJoyCon(vendor_id, product_id, serial)

    if pairing_method == PairingMethod.OLD.value:
        protocol_version = WsSubprotocolVersion.V1
    else:
        protocol_version = WsSubprotocolVersion.V2

    joydance = JoyDance(
        joycon,
        protocol_version=protocol_version,
        pairing_code=pairing_code,
        host_ip_addr=host_ip_addr,
        console_ip_addr=console_ip_addr,
        on_state_changed=on_joydance_state_changed,
        on_game_message=on_game_message,
    )
    app['joydance_connections'][serial] = joydance

    asyncio.create_task(joydance.pair())


async def disconnect_joycon(app, ws, data):
    print(data)
    serial = data['joycon_serial']
    joydance = app['joydance_connections'][serial]
    await joydance.disconnect()


async def on_startup(app):
    print(f'''
     ░░  ░░░░░░  ░░    ░░ ░░░░░░   ░░░░░  ░░░    ░░  ░░░░░░ ░░░░░░░
     ▒▒ ▒▒    ▒▒  ▒▒  ▒▒  ▒▒   ▒▒ ▒▒   ▒▒ ▒▒▒▒   ▒▒ ▒▒      ▒▒
     ▒▒ ▒▒    ▒▒   ▒▒▒▒   ▒▒   ▒▒ ▒▒▒▒▒▒▒ ▒▒ ▒▒  ▒▒ ▒▒      ▒▒▒▒▒
▓▓   ▓▓ ▓▓    ▓▓    ▓▓    ▓▓   ▓▓ ▓▓   ▓▓ ▓▓  ▓▓ ▓▓ ▓▓      ▓▓
 █████   ██████     ██    ██████  ██   ██ ██   ████  ██████ ███████

Running version {JOYDANCE_VERSION}''')

    # Indicate where we read/save configs
    if app['loaded_cfg_path']:
        if app['loaded_cfg_path'] == app['saved_cfg_path']:
            print('Loading & saving config to:', app['loaded_cfg_path'])
        else:
            print('Loaded config from:', app['loaded_cfg_path'])
    if app['saved_cfg_path'] and app['loaded_cfg_path'] != app['saved_cfg_path']:
        print('Created new config at:', app['saved_cfg_path'])

    # Check for update
    async def get_latest_tag_from_api_and_compare(api_endpoint: str) -> bool:
        try:
            async with session.get(api_endpoint, ssl=False) as resp:
                if resp.status == 404:
                    return False
                json_body = await resp.json()
                if isinstance(json_body, dict):
                    # parse from latest version
                    latest_version = json_body['tag_name'][1:]
                else:
                    # parse from list of tags
                    latest_version = json_body[0]['name'][1:]
                if JOYDANCE_VERSION != latest_version:
                    print('\033[93m{}\033[00m'.format('Version {} is available: https://github.com/kzvdar42/joydance'.format(latest_version)))
                return True
        except:
            return False

    # Firstly check releases page, then page with tags
    async with aiohttp.ClientSession() as session:
        if await get_latest_tag_from_api_and_compare('https://api.github.com/repos/kzvdar42/joydance/releases/latest'):
            return
        if not await get_latest_tag_from_api_and_compare('https://api.github.com/repos/kzvdar42/joydance/tags'):
            print('Error: Unable to fetch the latest release information. Please check the repository URL or your internet connection.')


async def html_handler(request):
    config = request.app['config_handler'].data
    with open(get_static_path('static/index.html'), 'r') as f:
        html = f.read()
        html = html.replace('[[CONFIG]]', json.dumps(config))
        html = html.replace('[[VERSION]]', JOYDANCE_VERSION)
        return web.Response(text=html, content_type='text/html')


async def ws_send_response(ws, cmd, data):
    resp = {
        'cmd': 'resp_' + cmd.value,
        'data': data,
    }
    await ws.send_json(resp)


async def toggle_rumble(app, ws, data):
    serial = data['joycon_serial']
    enabled = data['enabled']
    if serial in app['joydance_connections']:
        joydance = app['joydance_connections'][serial]
        await joydance.set_rumble_enabled(enabled)


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    async for msg in ws:
        print('got ws msg', msg)
        if msg.type == WSMsgType.TEXT:
            try:
                msg_data = msg.json()
                cmd = WsCommand(msg_data['cmd'])
                data = msg_data.get('data', {})
            except (ValueError, KeyError) as e:
                print(f'Invalid message: {e}')
                print(f'Message content: {msg.data}')
                continue

            try:
                if cmd == WsCommand.SEARCH_INPUT:
                    text = data.get('text', '')
                    # TODO: use main joycon?
                    serial = next(iter(request.app['joydance_connections']))
                    joydance = request.app['joydance_connections'][serial]
                    if joydance.is_search_opened:
                        await joydance.send_message('JD_SubmitKeyboard_PhoneCommandData', {
                            'keyboardOutput': text
                        })
                elif cmd == WsCommand.GET_JOYCON_LIST:
                    joycon_list = await get_joycon_list(request.app)
                    await ws_send_response(ws, cmd, joycon_list)
                elif cmd == WsCommand.CONNECT_JOYCON:
                    await connect_joycon(request.app, ws, data)
                    await ws_send_response(ws, cmd, {})
                elif cmd == WsCommand.DISCONNECT_JOYCON:
                    await disconnect_joycon(request.app, ws, data)
                    await ws_send_response(ws, cmd, {})
                elif cmd == WsCommand.TOGGLE_RUMBLE:
                    await toggle_rumble(request.app, ws, data)
            except Exception as e:
                print(f"Error handling command {cmd}: {e}")
                # Send error response to client
                await ws_send_response(ws, cmd, {
                    'error': str(e),
                    'status': 'error'
                })
        elif msg.type == WSMsgType.ERROR:
            print(f'ws connection closed with exception {ws.exception()}')

    return ws


def favicon_handler(request):
    return web.FileResponse(get_static_path('static/favicon.png'))


def get_static_path(relative_path):
    if getattr(sys, 'frozen', False):
        # If the application is frozen (running as an executable)
        base_path = sys._MEIPASS  # This is where PyInstaller unpacks the files
    else:
        # If the application is running in a normal Python environment
        base_path = os.path.dirname(__file__)
    
    return os.path.join(base_path, relative_path)


if __name__ == '__main__':
    app = web.Application()
    # Need to manually set media type mapping for js, as windows has a 
    # bug in which it sometimes parses .js files at "text/plain"
    mimetypes.init()
    mimetypes.types_map['.js'] = 'application/javascript'

    # Define app variables and load&save config
    app['joydance_connections'] = {}
    app['joycons_info'] = {}
    app['config_handler'] = ConfigHandler(CONFIG_PATHS)
    app['loaded_cfg_path'] = app['config_handler'].current_cfg_path
    app['config_handler'].save_data()
    app['saved_cfg_path'] = app['config_handler'].current_cfg_path

    app.on_startup.append(on_startup)
    app.add_routes([
        web.get('/', html_handler),
        web.get('/favicon.png', favicon_handler),
        web.get('/ws', websocket_handler),
        web.static('/css', get_static_path('static/css')),
        web.static('/js', get_static_path('static/js')),
    ])

    web.run_app(app, host='localhost', port=32623)
