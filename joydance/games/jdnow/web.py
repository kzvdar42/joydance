"""Just Dance Now integration with JoyDance's browser UI."""

import logging

from aiohttp import web

from joydance.constants import WsCommand
from joydance.utils import GameConnectionError
from .game import JustDanceGameNow
from .signin import open_sign_in_window, recover_profile


logger = logging.getLogger(__name__)


def _guests_in_use(app, excluding=None):
    return {game.reserved_guest_id for serial, game in app["joydance_connections"].items()
            if serial != excluding and isinstance(game, JustDanceGameNow)} - {None}


def require_available_guest(app, serial, player_id):
    if player_id is not None and player_id in _guests_in_use(app, excluding=serial):
        raise GameConnectionError("That Just Dance Now guest is already in use.", should_reconnect=False)


def guest_states(app, serials):
    try:
        return app["config_handler"].get_jdnow_guest_states(serials, _guests_in_use(app))
    except ValueError as error:
        logger.warning("Could not load saved JDNow guests: %s", error)
        return {serial: {"jdnow_guests": [], "jdnow_guest": None} for serial in serials}


async def broadcast_guest_changes(app, broadcast, *, excluding=None):
    changed = []
    for serial, guest_state in guest_states(app, app["controllers_info"]).items():
        info = app["controllers_info"][serial]
        if any(info.get(key) != value for key, value in guest_state.items()):
            info.update(guest_state)
            if serial != excluding:
                changed.append(info)
    for info in changed:
        await broadcast(app, WsCommand.UPDATE_CONTROLLER_STATE, info)


async def sign_in_handler(request, *, port, app_path, broadcast):
    host = request.host
    if (request.remote != "127.0.0.1" or host not in {f"localhost:{port}", f"127.0.0.1:{port}"}
            or request.headers.get("Origin") != f"http://{host}"):
        return web.json_response({"error": "Open JoyDance on this computer to sign in."}, status=403)

    app = request.app
    sign_in_lock = app["jdnow_sign_in_lock"]
    if sign_in_lock.locked():
        return web.json_response({"error": "A Ubisoft sign-in is already open."}, status=409)
    async with sign_in_lock:
        if _guests_in_use(app):
            return web.json_response({"error": "Disconnect JDNow controllers before signing in."}, status=409)
        try:
            user_id = await open_sign_in_window(app_path)
        except RuntimeError as error:
            return web.json_response({"error": str(error)}, status=503)
        if user_id is None:
            return web.json_response({"error": "Ubisoft sign-in closed or timed out."}, status=400)

        async with app["jdnow_guest_lock"]:
            if _guests_in_use(app):
                return web.json_response({"error": "Disconnect JDNow controllers before signing in."}, status=409)
            try:
                player_id, name = await recover_profile(user_id)
            except GameConnectionError as error:
                return web.json_response({"error": str(error)}, status=502)
            except (OSError, ValueError, KeyError, TypeError) as error:
                logger.warning("JDNow linked-profile response could not be read (%s).", type(error).__name__)
                return web.json_response({"error": "Could not read the linked dancer profile from JDNow."}, status=502)
            try:
                app["config_handler"].save_jdnow_linked_profile(player_id, name)
            except (OSError, ValueError):
                return web.json_response({"error": "JDNow profile recovered, but JoyDance could not save it to config.cfg."},
                                         status=503)
        await broadcast_guest_changes(app, broadcast)
        return web.json_response({})


async def handle_action(game, ws, cmd, data, *, send_response):
    serial = data["controller_serial"]
    try:
        if cmd == WsCommand.SEARCH_INPUT:
            await game.search_songs(data.get("text", ""))
        elif cmd == WsCommand.SELECT_SONG:
            await game.select_song(data.get("song"))
        else:
            name = data.get("name")
            await game.rename_player(name)
            await send_response(ws, cmd, {"serial": serial, "submitted_name": name})
    except (ValueError, GameConnectionError) as error:
        logger.warning("%s: JDN web action=%s failed: %s", serial, cmd.value, error)
        field = "player_rename_feedback" if cmd == WsCommand.RENAME_PLAYER else "song_search_feedback"
        await game.on_state_changed({field: {"text": str(error), "error": True}})
