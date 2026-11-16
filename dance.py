import asyncio
from collections import defaultdict
from contextlib import nullcontext
from functools import partial
import json
import logging
import os
import re
import sys
import mimetypes
import argparse
from multiprocessing import freeze_support
from urllib.parse import urlparse

if __name__ == "__main__":
    # Frozen scoring workers must branch off before importing the web server.
    freeze_support()

import aiohttp
from aiohttp import WSMsgType, web

from joydance.config_handler import PAIRING_RULES, ConfigHandler, get_config_paths, normalize_pairing_code
from joydance.constants import (
    WsCommand,
    PairingState,
    PairingMethod,
    JOYDANCE_VERSION,
    BASE_CONTROLLER_STATE_INFO,
)
from joydance.controllers import update_controllers_list
from joydance.games.factory import create_game
from joydance.games.jdnow import web as jdnow_web
from joydance.games.jdnow.game import EMPTY_SONG_SEARCH, JustDanceGameNow
from joydance.games.jdnow.workers import JDNowScoringWorkers
from joydance.games.jdnow.assets import JDNowAssets
from joydance.games.jdnow.signin import run_sign_in_window
from joydance.games.discovery import discover_consoles
from joydance.utils.network import normalize_console_url

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


CONFIG_PATHS = get_config_paths(__file__)
WEB_PORT = 32623


async def update_controllers_info(app):
    await update_controllers_list(
        app["controllers"], get_controller_settings=app["config_handler"].get_controller_settings,
    )
    controllers_info = []
    for controller in app["controllers"].values():
        try:
            if controller.serial not in app["joydance_connections"]:
                # Add base info fields for controllers that are not connected yet
                controller_info = BASE_CONTROLLER_STATE_INFO.copy()
                controller_info.update(await controller.get_state())
            else:
                controller_info = await app["joydance_connections"][controller.serial].get_state()

            app["controllers_info"][controller.serial].update(controller_info)
            controllers_info.append(controller_info)
        except Exception as e:
            logger.error(f"Error initializing {controller.__class__.__name__} {controller.serial}: {e}", exc_info=True)
            continue

    guest_states = jdnow_web.guest_states(app, [info["serial"] for info in controllers_info])
    for info in controllers_info:
        serial = info["serial"]
        info.update(guest_states[serial])
        app["controllers_info"][serial].update(guest_states[serial])
    return sorted(controllers_info, key=lambda x: (x["name"], x["color"], x["serial"]))


async def connect_controller(app, ws, data) -> None:
    scoring_workers = app.get("jdnow_scoring_workers")

    def save_jdnow_guest(player_id, name):
        if app["joydance_connections"].get(serial) is game_connection:
            jdnow_web.require_available_guest(app, serial, player_id)
            config_handler.save_jdnow_player_id(serial, player_id, name)

    async def on_joydance_state_changed(serial, update_dict):
        try:
            app["controllers_info"][serial].update(update_dict)
            state = update_dict.get("state")
            if is_jdnow:
                if scoring_workers is not None and state == PairingState.CONNECTED.value:
                    scoring_workers.prepare(sum(game.is_connected for game in app["joydance_connections"].values()
                                                if isinstance(game, JustDanceGameNow)))
                await jdnow_web.broadcast_guest_changes(app, ws_broadcast_response, excluding=serial)
            await ws_broadcast_response(app, WsCommand.UPDATE_CONTROLLER_STATE, app["controllers_info"][serial])
            if state is not None and (state == PairingState.DISCONNECTED.value or state >= PairingState.ERROR_CONTROLLER.value):
                await ws_broadcast_response(app, WsCommand.HIDE_SEARCH, {"serial": serial})
        except Exception as e:
            logger.error("Error in on_joydance_state_changed: %s", e, exc_info=True)

    async def on_game_message(message):
        try:
            __class = message.get("__class")
            if __class == "JD_OpenPhoneKeyboard_ConsoleCommandData":
                await ws_broadcast_response(app, WsCommand.SHOW_SEARCH, {
                    "serial": serial, "isPassword": bool(message.get("isPassword")),
                })
            elif __class in {"JD_CancelKeyboard_ConsoleCommandData", "JD_ClosePopup_ConsoleCommandData"} or (
                __class == "JD_PhoneUiSetupData" and message.get("isPopup")
                and isinstance(message.get("setupData"), dict)
            ):
                await ws_broadcast_response(app, WsCommand.HIDE_SEARCH, {"serial": serial})
        except Exception as e:
            logger.error("Error in on_game_message: %s", e, exc_info=True)

    try:
        logger.debug("connect_controller: %s", data)

        serial = data["controller_serial"]
        controller = app["controllers"][serial]

        pairing_method = data["pairing_method"]
        is_jdnow = pairing_method == PairingMethod.JDNOW.value
        host_ip_addr = data["host_ip_addr"]
        console_ip_addr = data["console_ip_addr"]
        pairing_code = data["pairing_code"]
        console_options = {}
        if data.get("console_url") is not None:
            if pairing_method != PairingMethod.OLD.value:
                raise ValueError("Discovered addresses are only used for legacy connections.")
            console_url = normalize_console_url(data["console_url"])
            if urlparse(console_url).hostname != console_ip_addr:
                raise ValueError("Discovered address does not match the console IP.")
            console_options["console_url"] = console_url

        config_handler = app["config_handler"]
        is_valid, error_msg = config_handler.validate_config_with_error(data)
        if not is_valid:
            logger.error(f"Configuration validation failed: {error_msg}")
            raise ValueError(f"Configuration validation failed: {error_msg}")
        pairing_code = normalize_pairing_code(pairing_code, pairing_method)
        uses_pairing_code = PAIRING_RULES["methods"][pairing_method]["code"] is not None
        async with app["joydance_connection_locks"][serial]:
            if is_jdnow:
                player_id = config_handler.get_jdnow_player_id(serial, data.get("jdnow_guest"))
                jdnow_web.require_available_guest(app, serial, player_id)
                console_options["player_id"] = player_id
                console_options["on_guest"] = save_jdnow_guest
                console_options["scoring_workers"] = scoring_workers
                console_options["assets"] = app.get("jdnow_assets")

            game_connection = create_game(
                controller=controller,
                pairing_code=pairing_code,
                host_ip_addr=host_ip_addr,
                console_ip_addr=None if uses_pairing_code else console_ip_addr,
                on_state_changed=on_joydance_state_changed,
                on_game_message=on_game_message,
                pairing_method=pairing_method,
                **console_options,
            )
            logger.debug("%s: connect_controller - created %s instance", serial, type(game_connection).__name__)

            previous_connection = app["joydance_connections"].get(serial)
            if previous_connection is not None:
                await previous_connection.disconnect(should_reconnect=False)

            guest_lock = app["jdnow_guest_lock"] if is_jdnow else nullcontext()
            async with guest_lock:
                if is_jdnow:
                    jdnow_web.require_available_guest(app, serial, player_id)

                config = config_handler.data.copy()
                config["pairing_code"] = pairing_code
                config["pairing_method"] = pairing_method
                config["host_ip_addr"] = host_ip_addr
                config["console_ip_addr"] = console_ip_addr
                config_handler.data = config

                app["controllers_info"].setdefault(serial, {}).update({
                    "pairing_code": pairing_code if uses_pairing_code else "",
                    "supports_song_search": False, "supports_player_rename": False,
                    "song_search_enabled": False, **EMPTY_SONG_SEARCH,
                    "player_color": None, "additional_message": None, "player_rename_feedback": None,
                })
                app["joydance_connections"][serial] = game_connection

                logger.debug(f"{serial}: connect_controller - starting pair() task")
                task = await game_connection.start_pair()
            task.add_done_callback(handle_task_exception)
            logger.debug(f"{serial}: connect_controller - completed successfully")
    except Exception as e:
        logger.error(f"Error in connect_controller: {e}", exc_info=True)
        raise


async def disconnect_controller(app, ws, data):
    try:
        logger.debug("disconnect_controller: %s", data)
        serial = data["controller_serial"]
        async with app["joydance_connection_locks"][serial]:
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
    # Windows redirects stdout using a legacy encoding that cannot print the banner.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
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
        except Exception:
            return False

    # Firstly check releases page, then page with tags
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
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


async def on_cleanup(app):
    await app["jdnow_assets"].close()


async def html_handler(request):
    config_handler = request.app["config_handler"]
    values = {
        "CONFIG": config_handler.data,
        "CONNECTIONS": config_handler.connections,
        "PAIRING_RULES": PAIRING_RULES,
        "VERSION": JOYDANCE_VERSION,
    }
    with open(get_static_path("static/index.html"), "r", encoding="utf-8") as f:
        html = re.sub(
            r"\[\[(CONFIG|CONNECTIONS|PAIRING_RULES|VERSION)\]\]",
            lambda match: json.dumps(values[match[1]]).replace("<", "\\u003c"),
            f.read(),
        )
    return web.Response(text=html, content_type="text/html")


async def discovery_handler(request):
    try:
        consoles = await discover_consoles(request.app["config_handler"].data["host_ip_addr"])
        return web.json_response({"consoles": consoles})
    except OSError:
        logger.debug("Local console discovery unavailable", exc_info=True)
        return web.json_response({
            "error": "Could not scan the network. Close other controller apps using port 7000 and try again.",
        }, status=503)


async def connections_handler(request):
    config = request.app["config_handler"]
    try:
        if request.method == "POST":
            if request.content_type != "application/json":
                raise ValueError("Expected connection settings as JSON.")
            connection = config.save_connection(await request.json())
        elif request.method == "DELETE":
            config.delete_connection(request.match_info["id"])
        else:
            return web.json_response({"connections": config.connections})
        await ws_broadcast_response(request.app, WsCommand.GET_CONNECTIONS, config.connections)
        response = {"connections": config.connections}
        if request.method == "POST":
            response["connection"] = connection
        return web.json_response(response)
    except (ValueError, TypeError) as error:
        return web.json_response({"error": str(error)}, status=400)
    except KeyError as error:
        return web.json_response({"error": error.args[0]}, status=404)
    except OSError as error:
        return web.json_response({"error": str(error)}, status=503)


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


async def ws_broadcast_response(app, cmd, data):
    for ws in tuple(app["websockets"]):
        if ws.closed:
            continue
        try:
            await ws_send_response(ws, cmd, data)
        except Exception:
            logger.exception("Error sending browser update: %s", cmd.value)


async def toggle_rumble(app, ws, data):
    try:
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            raise ValueError("Invalid rumble setting.")
        serial = data.get("controller_serial")
        enabled = data["enabled"]
        logger.debug(f"toggle_rumble: serial={serial}, enabled={enabled}")

        if not isinstance(serial, str) or serial not in app["controllers"]:
            logger.warning(f"Attempted to toggle rumble for unknown controller: {serial}")
            return

        target = app["joydance_connections"].get(serial, app["controllers"][serial])
        target.set_rumble(enabled)
        # Update the info for UI
        if serial in app["controllers_info"]:
            app["controllers_info"][serial]["rumble_enabled"] = enabled
            # Send update to client
            await ws_broadcast_response(app, WsCommand.UPDATE_CONTROLLER_STATE, app["controllers_info"][serial])
        logger.debug(f"{serial}: toggle_rumble - completed successfully")
    except Exception as e:
        logger.error(f"Error in toggle_rumble: {e}", exc_info=True)
        raise


async def save_controller_settings(app, data):
    if not isinstance(data, dict):
        raise ValueError("Invalid controller settings.")
    serial = data.get("controller_serial")
    if not isinstance(serial, str) or serial not in app["controllers"]:
        raise ValueError("Unknown controller.")
    async with app["joydance_connection_locks"][serial]:
        settings = data.get("settings")
        app["config_handler"].save_controller_settings(serial, settings)
        controller = app["controllers"][serial]
        await controller.apply_motion_settings(**settings)
        game = app["joydance_connections"].get(serial)
        state = await game.get_state() if game is not None else await controller.get_state()
        app["controllers_info"][serial].update(state)
        await ws_broadcast_response(app, WsCommand.UPDATE_CONTROLLER_STATE, app["controllers_info"][serial])


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    try:
        await ws.prepare(request)
        request.app["websockets"].add(ws)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                cmd = None
                try:
                    msg_data = msg.json()
                    cmd = WsCommand(msg_data["cmd"])
                    data = msg_data.get("data", {})
                    logger.debug("got ws command %s", cmd.value)
                except (ValueError, KeyError) as e:
                    logger.error("Invalid message format: %s", e)
                    continue
                except Exception as e:
                    logger.error("Unexpected error parsing message: %s", e, exc_info=True)
                    continue

                try:
                    if cmd == WsCommand.GET_CONNECTIONS:
                        await ws_send_response(ws, cmd, request.app["config_handler"].connections)
                    elif cmd in {WsCommand.SEARCH_INPUT, WsCommand.SELECT_SONG, WsCommand.RENAME_PLAYER}:
                        serial = data.get("controller_serial")
                        if not isinstance(serial, str) or serial not in request.app["joydance_connections"]:
                            logger.warning("%s received for unknown controller: %s", cmd.value, serial)
                            continue
                        joydance = request.app["joydance_connections"][serial]
                        if isinstance(joydance, JustDanceGameNow):
                            await jdnow_web.handle_action(joydance, ws, cmd, data, send_response=ws_send_response)
                        elif cmd == WsCommand.SEARCH_INPUT and joydance.is_connected and joydance.is_search_opened:
                            await joydance.send_message(
                                "JD_SubmitKeyboard_PhoneCommandData", {"keyboardOutput": data.get("text", "")}
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
                    elif cmd == WsCommand.SAVE_CONTROLLER_SETTINGS:
                        result = {"controller_serial": data.get("controller_serial")} if isinstance(data, dict) else {}
                        try:
                            await save_controller_settings(request.app, data)
                        except (ValueError, OSError) as error:
                            result["error"] = str(error)
                        await ws_send_response(ws, cmd, result)
                    else:
                        logger.warning(f"Unknown command: {cmd}")
                except Exception as e:
                    logger.error("Error handling command %s: %s", cmd, e, exc_info=True)
                    # Send error response to client
                    try:
                        error_response = {"error": str(e), "status": "error"}
                        if cmd == WsCommand.SAVE_CONTROLLER_SETTINGS and isinstance(data, dict):
                            error_response["controller_serial"] = data.get("controller_serial")
                        await ws_send_response(ws, cmd, error_response)
                    except Exception as send_error:
                        logger.error("Failed to send error response: %s", send_error, exc_info=True)
            elif msg.type == WSMsgType.ERROR:
                logger.error("ws connection closed with exception %s", ws.exception())
    except Exception as e:
        logger.error("Fatal error in websocket_handler: %s", e, exc_info=True)
    finally:
        request.app["websockets"].discard(ws)
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
    parser.add_argument("--jdnow-sign-in-window", action="store_true", help=argparse.SUPPRESS)

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
    if args.jdnow_sign_in_window:
        sys.exit(run_sign_in_window())

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

    scoring_workers = JDNowScoringWorkers()
    try:
        app = web.Application()
        # Need to manually set media type mapping for js, as windows has a
        # bug in which it sometimes parses .js files at "text/plain"
        mimetypes.init()
        mimetypes.types_map[".js"] = "application/javascript"

        # Define app variables and load&save config
        app["joydance_connections"] = {}
        app["websockets"] = set()
        app["joydance_connection_locks"] = defaultdict(asyncio.Lock)
        app["jdnow_guest_lock"] = asyncio.Lock()
        app["jdnow_sign_in_lock"] = asyncio.Lock()
        app["jdnow_scoring_workers"] = scoring_workers
        app["jdnow_assets"] = JDNowAssets()
        app["controllers"] = {}
        app["controllers_info"] = defaultdict(dict)
        app["config_handler"] = ConfigHandler(CONFIG_PATHS)
        app["loaded_cfg_path"] = app["config_handler"].current_cfg_path
        app["config_handler"].save_data()
        app["saved_cfg_path"] = app["config_handler"].current_cfg_path

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        app.add_routes(
            [
                web.get("/", html_handler),
                web.get("/favicon.png", favicon_handler),
                web.get("/ws", websocket_handler),
                web.get("/discovery", discovery_handler),
                web.get("/connections", connections_handler),
                web.post("/connections", connections_handler),
                web.post("/jdnow/sign-in", partial(
                    jdnow_web.sign_in_handler, port=WEB_PORT, app_path=os.path.abspath(__file__),
                    broadcast=ws_broadcast_response,
                )),
                web.delete("/connections/{id}", connections_handler),
                web.static("/css", get_static_path("static/css")),
                web.static("/js", get_static_path("static/js")),
            ]
        )

        web.run_app(
            app,
            host="0.0.0.0",
            port=WEB_PORT,
            print=lambda *args: print("======== Running on http://localhost:32623 ========"),
        )
    except KeyboardInterrupt:
        logger.info("Application stopped by user (KeyboardInterrupt)")
    except Exception as e:
        logger.critical("Critical error in main: %s", e, exc_info=True)
        raise
    finally:
        scoring_workers.close()
