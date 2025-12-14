import asyncio
from collections import defaultdict
import json
import logging
import os
import sys
import mimetypes
import argparse

import aiohttp
from aiohttp import WSMsgType, web

from joydance.config_handler import ConfigHandler, get_datadir
from joydance.constants import (
    WsCommand,
    PairingMethod,
    JOYDANCE_VERSION,
    BASE_CONTROLLER_STATE_INFO,
)
from joydance.controllers import update_controllers_list
from joydance.games.justdance_v1 import JustDanceGameV1
from joydance.games.justdance_v2 import JustDanceGameV2

logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger("dance")


def handle_task_exception(task):
    """Callback to handle exceptions in background tasks."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass  # Task cancellation is expected
    except Exception as e:
        logger.error("Unhandled exception in background task: %s", e, exc_info=True)


CONFIG_PATHS = ["config.cfg", os.path.join(get_datadir(), "config.cfg")]


async def update_controllers_info(app):
    await update_controllers_list(app["controllers"])
    controllers_info = []
    for controller in app["controllers"].values():
        try:
            battery_level = await controller.battery_level()
            color = "#%02x%02x%02x" % controller.color_body

            controller_info = {
                "vendor_id": controller.vendor_id,
                "product_id": controller.product_id,
                "serial": controller.serial,
                "name": controller.name,
                "color": color,
                "battery_level": battery_level,
                "is_left": controller.is_left(),
                "rumble_enabled": controller.rumble_enabled,
            }

            # Add base info fields for controllers that are not connected yet
            if controller.serial not in app["joydance_connections"]:
                controller_info.update(BASE_CONTROLLER_STATE_INFO)

            app["controllers_info"][controller.serial].update(controller_info)
            controllers_info.append(controller_info)
        except Exception as e:
            logger.error(f"Error initializing {controller.__class__.__name__} {controller.serial}: {e}", exc_info=True)
            continue

    return sorted(controllers_info, key=lambda x: (x["name"], x["color"], x["serial"]))

async def connect_controller(app, ws, data) -> None:
    async def on_joydance_state_changed(serial, update_dict):
        try:
            app["controllers_info"][serial].update(update_dict)
            await ws_send_response(ws, WsCommand.UPDATE_CONTROLLER_STATE, app["controllers_info"][serial])
        except Exception as e:
            logger.error("Error in on_joydance_state_changed: %s", e, exc_info=True)

    async def on_game_message(message):
        try:
            __class = message.get("__class")
            if __class == "JD_OpenPhoneKeyboard_ConsoleCommandData":
                await ws_send_response(ws, WsCommand.SHOW_SEARCH, {"serial": serial})
            elif __class == "JD_CancelKeyboard_ConsoleCommandData":
                await ws_send_response(ws, WsCommand.HIDE_SEARCH, {"serial": serial})
        except Exception as e:
            logger.error("Error in on_game_message: %s", e, exc_info=True)

    try:
        logger.debug("connect_controller: %s", data)

        serial = data["controller_serial"]
        controller = app["controllers"][serial]

        pairing_method = data["pairing_method"]
        host_ip_addr = data["host_ip_addr"]
        console_ip_addr = data["console_ip_addr"]
        pairing_code = data["pairing_code"]

        is_valid, error_msg = app["config_handler"].validate_config_with_error(data)
        if not is_valid:
            logger.error(f"Configuration validation failed: {error_msg}")
            raise ValueError(f"Configuration validation failed: {error_msg}")

        config = app["config_handler"].data.copy()
        config["pairing_code"] = pairing_code
        config["pairing_method"] = pairing_method
        config["host_ip_addr"] = host_ip_addr
        config["console_ip_addr"] = console_ip_addr
        app["config_handler"].data = config

        if pairing_method in {PairingMethod.DEFAULT.value, PairingMethod.STADIA.value}:
            app["controllers_info"][serial]["pairing_code"] = pairing_code
            console_ip_addr = None
        else:
            app["controllers_info"][serial]["pairing_code"] = ""

        if pairing_method == PairingMethod.OLD.value:
            game_class = JustDanceGameV1
        else:
            game_class = JustDanceGameV2

        logger.debug(f"{serial}: connect_controller - creating {game_class.__name__} instance")
        game_connection = game_class(
            controller=controller,
            pairing_code=pairing_code,
            host_ip_addr=host_ip_addr,
            console_ip_addr=console_ip_addr,
            on_state_changed=on_joydance_state_changed,
            on_game_message=on_game_message,
        )

        app["joydance_connections"][serial] = game_connection

        logger.debug(f"{serial}: connect_controller - starting pair() task")
        task = asyncio.create_task(game_connection.pair())
        task.add_done_callback(handle_task_exception)
        logger.debug(f"{serial}: connect_controller - completed successfully")
    except Exception as e:
        logger.error(f"Error in connect_controller: {e}", exc_info=True)
        raise


async def disconnect_controller(app, ws, data):
    try:
        logger.debug("disconnect_controller: %s", data)
        serial = data["controller_serial"]
        if serial not in app["joydance_connections"]:
            logger.warning(f"Attempted to disconnect unknown controller: {serial}")
            return
        joydance = app["joydance_connections"][serial]
        await joydance.disconnect(should_reconnect=False)
        logger.debug(f"{serial}: disconnect_controller - completed successfully")
    except Exception as e:
        logger.error(f"Error in disconnect_controller: {e}", exc_info=True)
        raise


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
    try:
        serial = data["controller_serial"]
        enabled = data["enabled"]
        logger.debug(f"toggle_rumble: serial={serial}, enabled={enabled}")

        if serial not in app["joydance_connections"]:
            logger.warning(f"Attempted to toggle rumble for unknown controller: {serial}")
            return

        joydance = app["joydance_connections"][serial]
        joydance.set_rumble(enabled)
        # Update the info for UI
        if serial in app["controllers_info"]:
            app["controllers_info"][serial]["rumble_enabled"] = enabled
            # Send update to client
            await ws_send_response(ws, WsCommand.UPDATE_CONTROLLER_STATE, app["controllers_info"][serial])
        logger.debug(f"{serial}: toggle_rumble - completed successfully")
    except Exception as e:
        logger.error(f"Error in toggle_rumble: {e}", exc_info=True)
        raise


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    try:
        await ws.prepare(request)
        async for msg in ws:
            logger.debug("got ws msg %s", msg)
            if msg.type == WSMsgType.TEXT:
                cmd = None
                try:
                    msg_data = msg.json()
                    cmd = WsCommand(msg_data["cmd"])
                    data = msg_data.get("data", {})
                except (ValueError, KeyError) as e:
                    logger.error("Invalid message format: %s", e)
                    logger.error("Message content: %s", msg.data)
                    continue
                except Exception as e:
                    logger.error("Unexpected error parsing message: %s", e, exc_info=True)
                    continue

                try:
                    if cmd == WsCommand.SEARCH_INPUT:
                        text = data.get("text", "")
                        if not request.app["joydance_connections"]:
                            logger.warning("SEARCH_INPUT received but no controller connected")
                            continue
                        # TODO: use main controller?
                        serial = next(iter(request.app["joydance_connections"]))
                        joydance = request.app["joydance_connections"][serial]
                        if joydance.is_search_opened:
                            await joydance.send_message(
                                "JD_SubmitKeyboard_PhoneCommandData", {"keyboardOutput": text}
                            )
                    elif cmd == WsCommand.GET_CONTROLLER_LIST:
                        controllers_info = await update_controllers_info(request.app)
                        await ws_send_response(ws, cmd, controllers_info)
                    elif cmd == WsCommand.CONNECT_CONTROLLER:
                        await connect_controller(request.app, ws, data)
                        await ws_send_response(ws, cmd, {})
                    elif cmd == WsCommand.DISCONNECT_CONTROLLER:
                        await disconnect_controller(request.app, ws, data)
                        await ws_send_response(ws, cmd, {})
                    elif cmd == WsCommand.TOGGLE_RUMBLE:
                        await toggle_rumble(request.app, ws, data)
                    else:
                        logger.warning(f"Unknown command: {cmd}")
                except Exception as e:
                    logger.error("Error handling command %s: %s", cmd, e, exc_info=True)
                    # Send error response to client
                    try:
                        await ws_send_response(ws, cmd, {"error": str(e), "status": "error"})
                    except Exception as send_error:
                        logger.error("Failed to send error response: %s", send_error, exc_info=True)
            elif msg.type == WSMsgType.ERROR:
                logger.error("ws connection closed with exception %s", ws.exception())
    except Exception as e:
        logger.error("Fatal error in websocket_handler: %s", e, exc_info=True)
    finally:
        logger.debug("Websocket handler exiting")

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


def get_args():
    parser = argparse.ArgumentParser(prog="JoyDance")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--logs_filepath", default=None, help="Path to save logs to")

    return parser.parse_args()


def asyncio_exception_handler(loop, context):
    """Global exception handler for asyncio tasks."""
    exception = context.get("exception")
    if exception:
        logger.error(
            "Uncaught exception in asyncio task: %s",
            exception,
            exc_info=(type(exception), exception, exception.__traceback__)
        )
    else:
        logger.error("Uncaught exception in asyncio: %s", context.get("message", "Unknown error"))
        logger.error("Full context: %s", context)


if __name__ == "__main__":
    args = get_args()

    # Set up logging
    logging_level = logging.DEBUG if args.debug else logging.INFO
    logging_handlers = [logging.StreamHandler()]
    if args.logs_filepath:
        logging_handlers.append(logging.FileHandler(args.logs_filepath))
    logging.basicConfig(handlers=logging_handlers, level=logging_level)

    # Set up global asyncio exception handler
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(asyncio_exception_handler)

    logger.info("Starting JoyDance application...")

    try:
        app = web.Application()
        # Need to manually set media type mapping for js, as windows has a
        # bug in which it sometimes parses .js files at "text/plain"
        mimetypes.init()
        mimetypes.types_map[".js"] = "application/javascript"

        # Define app variables and load&save config
        app["joydance_connections"] = {}
        app["controllers"] = {}
        app["controllers_info"] = defaultdict(dict)
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
    except KeyboardInterrupt:
        logger.info("Application stopped by user (KeyboardInterrupt)")
    except Exception as e:
        logger.critical("Critical error in main: %s", e, exc_info=True)
        raise
