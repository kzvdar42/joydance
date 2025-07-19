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

from joydance.joydance_wrapper import JoyDance
from joydance.config_handler import ConfigHandler, get_datadir
from joydance.constants import (
    WsCommand,
    PairingMethod,
    JOYDANCE_VERSION,
    WsSubprotocolVersion,
    PairingState,
)
from pycon import ButtonEventJoyCon, JoyCon
from pycon.constants import JOYCON_PRODUCT_IDS, JOYCON_VENDOR_ID
from joydance.controllers.joycon_wrapper import JoyConWrapper
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2

logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger("dance")


CONFIG_PATHS = ["config.cfg", os.path.join(get_datadir(), "config.cfg")]


async def get_device_ids():
    devices = hid.enumerate(JOYCON_VENDOR_ID, 0)
    out = []
    for device in devices:
        vendor_id = device["vendor_id"]
        product_id = device["product_id"]
        product_string = device["product_string"]
        serial = device.get("serial") or device.get("serial_number")

        if product_id not in JOYCON_PRODUCT_IDS:
            continue

        if not product_string:
            continue

        out.append(
            {
                "vendor_id": vendor_id,
                "product_id": product_id,
                "serial": serial,
                "product_string": product_string,
            }
        )
    return out


async def get_joycon_list(app):
    joycons = []
    devices = await get_device_ids()

    for dev in devices:
        if dev["serial"] in app["joycons_info"]:
            info = app["joycons_info"][dev["serial"]]
        else:
            joycon = JoyCon(dev["vendor_id"], dev["product_id"], dev["serial"])
            # Wait for initial data
            for _ in range(3):
                time.sleep(0.05)
                battery_level = joycon.get_battery_level()
                if battery_level > 0:
                    break

            color = "#%02x%02x%02x" % joycon.color_body

            # Temporary fix for Windows
            if platform.system() != "Windows":
                joycon.__del__()

            info = {
                "vendor_id": dev["vendor_id"],
                "product_id": dev["product_id"],
                "serial": dev["serial"],
                "name": dev["product_string"],
                "color": color,
                "battery_level": battery_level,
                "is_left": joycon.is_left(),
                "state": PairingState.IDLE.value,
                "pairing_code": "",
                "rumble_enabled": joycon.rumble_enabled,
            }

            app["joycons_info"][dev["serial"]] = info

        joycons.append(info)
    return sorted(joycons, key=lambda x: (x["name"], x["color"], x["serial"]))


async def connect_joycon(app, ws, data) -> None:
    async def on_joydance_state_changed(serial, update_dict):
        app["joycons_info"][serial].update(update_dict)
        try:
            await ws_send_response(ws, WsCommand.UPDATE_JOYCON_STATE, app["joycons_info"][serial])
        except Exception as e:
            logger.error(e)

    async def on_game_message(message):
        __class = message.get("__class")
        if __class == "JD_OpenPhoneKeyboard_ConsoleCommandData":
            await ws_send_response(ws, WsCommand.SHOW_SEARCH, {"serial": serial})
        elif __class == "JD_CancelKeyboard_ConsoleCommandData":
            await ws_send_response(ws, WsCommand.HIDE_SEARCH, {"serial": serial})

    logger.debug("connect_joycon: %s", data)

    serial = data["joycon_serial"]
    product_id = app["joycons_info"][serial]["product_id"]
    vendor_id = app["joycons_info"][serial]["vendor_id"]

    pairing_method = data["pairing_method"]
    host_ip_addr = data["host_ip_addr"]
    console_ip_addr = data["console_ip_addr"]
    pairing_code = data["pairing_code"]

    if not app["config_handler"].is_new_config_valid(data):
        return

    config = app["config_handler"].data.copy()
    config["pairing_code"] = pairing_code
    config["pairing_method"] = pairing_method
    config["host_ip_addr"] = host_ip_addr
    config["console_ip_addr"] = console_ip_addr
    app["config_handler"].data = config

    if pairing_method == PairingMethod.DEFAULT.value or pairing_method == PairingMethod.STADIA.value:
        app["joycons_info"][serial]["pairing_code"] = pairing_code
        console_ip_addr = None
    else:
        app["joycons_info"][serial]["pairing_code"] = ""

    raw_joycon = ButtonEventJoyCon(vendor_id, product_id, serial)
    # Wrap the raw_joycon with JoyConWrapper
    controller = JoyConWrapper(raw_joycon)

    if pairing_method == PairingMethod.OLD.value:
        game_class = JustDanceGameV1
    else:
        game_class = JustDanceGameV2

    game_connection = game_class(
        controller=controller,
        pairing_code=pairing_code,
        host_ip_addr=host_ip_addr,
        console_ip_addr=console_ip_addr,
        on_state_changed=on_joydance_state_changed,
        on_game_message=on_game_message,
    )

    app["joydance_connections"][serial] = game_connection
    # Update rumble_enabled state in joycons_info from the controller
    if serial in app["joycons_info"] and hasattr(controller, "rumble_enabled"):
        app["joycons_info"][serial]["rumble_enabled"] = controller.rumble_enabled

    asyncio.create_task(game_connection.pair())


async def disconnect_joycon(app, ws, data):
    logger.debug("disconnect_joycon: %s", data)
    serial = data["joycon_serial"]
    joydance = app["joydance_connections"][serial]
    await joydance.disconnect()


async def on_startup(app):
    print(
        f"""
     ░░  ░░░░░░  ░░    ░░ ░░░░░░   ░░░░░  ░░░    ░░  ░░░░░░ ░░░░░░░
     ▒▒ ▒▒    ▒▒  ▒▒  ▒▒  ▒▒   ▒▒ ▒▒   ▒▒ ▒▒▒▒   ▒▒ ▒▒      ▒▒
     ▒▒ ▒▒    ▒▒   ▒▒▒▒   ▒▒   ▒▒ ▒▒▒▒▒▒▒ ▒▒ ▒▒  ▒▒ ▒▒      ▒▒▒▒▒
▓▓   ▓▓ ▓▓    ▓▓    ▓▓    ▓▓   ▓▓ ▓▓   ▓▓ ▓▓  ▓▓ ▓▓ ▓▓      ▓▓
 █████   ██████     ██    ██████  ██   ██ ██   ████  ██████ ███████

Running version {JOYDANCE_VERSION}"""
    )

    # Indicate where we read/save configs
    if app["loaded_cfg_path"]:
        if app["loaded_cfg_path"] == app["saved_cfg_path"]:
            print("Loading & saving config to:", app["loaded_cfg_path"])
        else:
            print("Loaded config from:", app["loaded_cfg_path"])
    if app["saved_cfg_path"] and app["loaded_cfg_path"] != app["saved_cfg_path"]:
        print("Created new config at:", app["saved_cfg_path"])

    # Check for update
    async def get_latest_tag_from_api_and_compare(api_endpoint: str) -> bool:
        try:
            async with session.get(api_endpoint, ssl=False) as resp:
                if resp.status == 404:
                    return False
                json_body = await resp.json()
                if isinstance(json_body, dict):
                    # parse from latest version
                    latest_version = json_body["tag_name"][1:]
                else:
                    # parse from list of tags
                    latest_version = json_body[0]["name"][1:]
                if JOYDANCE_VERSION != latest_version:
                    print(
                        "\033[93m{}\033[00m".format(
                            f"Version {latest_version} is available: https://github.com/kzvdar42/joydance"
                        )
                    )
                return True
        except:
            return False

    # Firstly check releases page, then page with tags
    async with aiohttp.ClientSession() as session:
        if await get_latest_tag_from_api_and_compare(
            "https://api.github.com/repos/kzvdar42/joydance/releases/latest"
        ):
            return
        if not await get_latest_tag_from_api_and_compare(
            "https://api.github.com/repos/kzvdar42/joydance/tags"
        ):
            print(
                "Error: Unable to fetch the latest release information. Please check the repository URL or your internet connection."
            )


async def html_handler(request):
    config = request.app["config_handler"].data
    with open(get_static_path("static/index.html"), "r", encoding="utf-8") as f:
        html = f.read()
        html = html.replace("[[CONFIG]]", json.dumps(config))
        html = html.replace("[[VERSION]]", JOYDANCE_VERSION)
        return web.Response(text=html, content_type="text/html")


async def ws_send_response(ws, cmd, data):
    resp = {
        "cmd": "resp_" + cmd.value,
        "data": data,
    }
    # Ensure ws is not closed before sending
    if not ws.closed:
        await ws.send_json(resp)
    else:
        logger.warning(f"Attempted to send to a closed websocket. CMD: {cmd.value}")


async def toggle_rumble(app, ws, data):
    serial = data["joycon_serial"]
    enabled = data["enabled"]
    if serial in app["joydance_connections"]:
        joydance = app["joydance_connections"][serial]
        joydance.set_rumble(enabled)  # Changed from set_rumble_enabled, and it's not async
        # Update the info for UI
        if serial in app["joycons_info"]:
            app["joycons_info"][serial]["rumble_enabled"] = enabled
            # Send update to client
            await ws_send_response(ws, WsCommand.UPDATE_JOYCON_STATE, app["joycons_info"][serial])


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    async for msg in ws:
        logger.debug("got ws msg %s", msg)
        if msg.type == WSMsgType.TEXT:
            try:
                msg_data = msg.json()
                cmd = WsCommand(msg_data["cmd"])
                data = msg_data.get("data", {})
            except (ValueError, KeyError) as e:
                logger.error("Invalid message: %s", e)
                logger.error("Message content: %s", msg.data)
                continue

            try:
                if cmd == WsCommand.SEARCH_INPUT:
                    text = data.get("text", "")
                    # TODO: use main joycon?
                    serial = next(iter(request.app["joydance_connections"]))
                    joydance = request.app["joydance_connections"][serial]
                    if joydance.is_search_opened:
                        await joydance.send_message(
                            "JD_SubmitKeyboard_PhoneCommandData", {"keyboardOutput": text}
                        )
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
                logger.error("Error handling command %s: %s", cmd, e)
                # Send error response to client
                await ws_send_response(ws, cmd, {"error": str(e), "status": "error"})
        elif msg.type == WSMsgType.ERROR:
            logger.error("ws connection closed with exception %s", ws.exception())

    return ws


def favicon_handler(request):
    return web.FileResponse(get_static_path("static/favicon.png"))


def get_static_path(relative_path):
    if getattr(sys, "frozen", False):
        # If the application is frozen (running as an executable)
        base_path = sys._MEIPASS  # This is where PyInstaller unpacks the files
    else:
        # If the application is running in a normal Python environment
        base_path = os.path.dirname(__file__)

    return os.path.join(base_path, relative_path)


if __name__ == "__main__":
    # set logging level based on --debug flag
    if len(sys.argv) > 1 and sys.argv[1] == "--debug":
        logging.basicConfig(level=logging.DEBUG)

    app = web.Application()
    # Need to manually set media type mapping for js, as windows has a
    # bug in which it sometimes parses .js files at "text/plain"
    mimetypes.init()
    mimetypes.types_map[".js"] = "application/javascript"

    # Define app variables and load&save config
    app["joydance_connections"] = {}
    app["joycons_info"] = {}
    app["config_handler"] = ConfigHandler(CONFIG_PATHS)
    app["loaded_cfg_path"] = app["config_handler"].current_cfg_path
    app["config_handler"].save_data()
    app["saved_cfg_path"] = app["config_handler"].current_cfg_path

    app.on_startup.append(on_startup)
    app.add_routes(
        [
            web.get("/", html_handler),
            web.get("/favicon.png", favicon_handler),
            web.get("/ws", websocket_handler),
            web.static("/css", get_static_path("static/css")),
            web.static("/js", get_static_path("static/js")),
        ]
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=32623,
        print=lambda *args: print("======== Running on http://localhost:32623 ========"),
    )
