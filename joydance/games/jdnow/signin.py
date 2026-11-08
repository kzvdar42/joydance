"""Ubisoft-hosted sign-in for a Just Dance Now profile."""
import asyncio
import sys
from threading import Event
import time
from urllib.parse import urlencode
from uuid import UUID

import aiohttp

from joydance.utils import GameConnectionError
from .api import JDNowAPI


UBISOFT_SIGN_IN_URL = "https://connect.ubisoft.com/connect-mobile/landing-page?" + urlencode({
    "appId": "cef33ce6-9b88-49a0-ac1d-f2d7cfe2d4b2",
    "genomeId": "5c494592-a14b-4b54-9197-24278c91ff55",
    "lang": "en-US",
})

# The hosted mobile flow exposes its result in the window instead of redirecting to a desktop callback.
READ_USER_ID = """
location.origin === 'https://connect.ubisoft.com' &&
window.ubisoftUnityLoginDataStatus === 'finished' ?
    window.ubisoftConnectLoginDataForUnity?.userId ?? null : null
"""


def sign_in():
    import webview
    from webview.errors import JavascriptException, WebViewException

    window = webview.create_window("Ubisoft Connect · Just Dance Now", UBISOFT_SIGN_IN_URL, width=560, height=760)
    closed = Event()
    user_id = None

    def read_result():
        nonlocal user_id
        deadline = time.monotonic() + 600
        while not closed.is_set() and time.monotonic() < deadline:
            if window.events.loaded.wait(0.25):
                try:
                    value = window.evaluate_js(READ_USER_ID)
                    if isinstance(value, str):
                        user_id = str(UUID(value))
                        window.destroy()
                        return
                except (JavascriptException, WebViewException, ValueError):
                    pass
            closed.wait(0.25)
        if not closed.is_set():
            window.destroy()

    window.events.closed += closed.set
    try:
        webview.start(read_result, gui="edgechromium", private_mode=True)
    except WebViewException as error:
        raise RuntimeError("Could not open the Ubisoft sign-in window. Check that Microsoft Edge WebView2 is installed.") from error
    return user_id


async def recover_profile(user_id):
    async with aiohttp.ClientSession() as session:
        api = JDNowAPI(session)
        await api.login()
        # JDNow can sync the linked account into the current dancer ID.
        await api.login(ubi_connect_id=user_id)
        resumed = JDNowAPI(session, api.player_id)
        await resumed.login()
        if resumed.player_id != api.player_id or resumed.profile["publicID"] != api.profile["publicID"]:
            raise GameConnectionError("JDNow did not restore the linked profile for desktop play.", should_reconnect=False)
        return resumed.player_id, resumed.profile.get("pName")


def run_sign_in_window():
    try:
        user_id = sign_in()
    except ImportError:
        return 2
    except RuntimeError:
        return 1
    if user_id:
        print(user_id, flush=True)
    return 0


async def open_sign_in_window(app_path):
    if sys.platform != "win32":
        raise RuntimeError("JDNow Ubisoft sign-in requires Windows and Microsoft Edge WebView2.")
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(app_path)
    try:
        process = await asyncio.create_subprocess_exec(
            *command, "--jdnow-sign-in-window", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(620):
                output, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    except TimeoutError:
        return None
    except OSError as error:
        raise RuntimeError("Could not open the Ubisoft sign-in window.") from error
    if process.returncode == 2:
        raise RuntimeError("Install pywebview in JoyDance’s Python environment to sign in.")
    if process.returncode != 0:
        raise RuntimeError("Could not open the Ubisoft sign-in window. Check Microsoft Edge WebView2.")
    try:
        return str(UUID(output.strip().decode("ascii"))) if output.strip() else None
    except ValueError as error:
        raise RuntimeError("Ubisoft sign-in returned an invalid result.") from error
