"""Guest sessions and public song data used by the JDN controller."""
import asyncio
import json
import logging
import re
from urllib.parse import urljoin, urlsplit

import aiohttp

from joydance.constants import JD_SERVICE_TIMEOUT, PairingState
from joydance.utils import GameConnectionError


APP_IDENTIFIER = "v54@3azv*FCqB6R6tnMM"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,128}")
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
PLAYER_NAME_LIMIT = 20
logger = logging.getLogger(__name__)


def service_url(value):
    if not isinstance(value, str):
        raise ValueError("Missing JDN service address")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".justdancenow.com")
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("Invalid JDN service address")
    return value


def parse_json(data, jsonp=False):
    text = data.decode("utf-8-sig").strip()
    if jsonp:
        wrapped = re.fullmatch(r"[a-zA-Z0-9_$]+\s*\((.*)\)\s*;?", text, re.DOTALL)
        if wrapped:
            text = wrapped.group(1)
    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")
    return json.loads(text, parse_constant=invalid_constant)


async def read_limited(response, limit):
    result = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        result.extend(chunk)
        if len(result) > limit:
            raise ValueError("JDN response is too large")
    return bytes(result)


def valid_player_name(name):
    return (isinstance(name, str) and bool(name.strip())
            and not re.search(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]", name)
            and len(name.strip().encode("utf-16-le")) <= 2 * PLAYER_NAME_LIMIT)


class JDNowAPI:
    def __init__(self, session, player_id=None, on_guest=None):
        self.session = session
        self.player_id = player_id or "Hello"
        self.on_guest = on_guest
        self.jdns = "https://jdns.justdancenow.com/"
        self.api = None
        self.auth_token = None
        self.profile = {}
        self._auth_lock = asyncio.Lock()

    async def request(self, method, url, *, authenticated=False, limit=8 * 1024 * 1024, accepted=(200,), **kwargs):
        service_url(url)
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(2):
            # Android guests are rejected by the room server; JoyDance is a desktop client.
            headers = {"x-platform": "pcapp"}
            rejected_token = self.auth_token
            if authenticated:
                if urlsplit(url).netloc != urlsplit(self.jdns).netloc or not self.auth_token:
                    raise ValueError("Invalid JDN authentication destination")
                headers["x-auth-token"] = "Bearer " + self.auth_token
            headers.update(extra_headers)
            try:
                async with self.session.request(method, url, headers=headers, allow_redirects=False,
                                                timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT), **kwargs) as response:
                    if response.status == 401 and authenticated and attempt == 0:
                        pass
                    elif response.status not in accepted:
                        raise GameConnectionError(
                            f"JDN service request failed (HTTP {response.status}).",
                            PairingState.ERROR_CONNECTION,
                            should_reconnect=response.status >= 500 or response.status in {408, 429},
                        )
                    else:
                        return await read_limited(response, limit)
            except (aiohttp.ClientError, TimeoutError) as error:
                raise GameConnectionError("Could not reach the JDN service.", PairingState.ERROR_CONNECTION) from error
            await self._refresh_auth(rejected_token)

    async def _refresh_auth(self, rejected_token):
        async with self._auth_lock:
            if self.auth_token != rejected_token:
                return
            logger.debug("JDN authenticated request rejected; renewing the guest session")
            refreshed = JDNowAPI(self.session, self.player_id)
            refreshed.jdns = self.jdns
            await refreshed.login()
            if (refreshed.player_id != self.player_id
                    or refreshed.profile["publicID"] != self.profile["publicID"]):
                raise ValueError("JDN guest identity changed. Reconnect to the game before renaming.")
            self.auth_token, self.profile, self.api = refreshed.auth_token, refreshed.profile, refreshed.api
            logger.debug("JDN guest session renewed")

    async def login(self, *, ubi_connect_id=None):
        token = parse_json(await self.request("GET", self.jdns + "getUserToken/", params={"_id": self.player_id}))
        if not isinstance(token.get("userToken"), str) or not 1 <= len(token["userToken"]) <= 4096:
            raise ValueError("Invalid JDN guest token")
        data = {
            "text": self.player_id, "firstPartyId": "", "teamPlayerId": "", "lang": "en", "userToken": token["userToken"],
        }
        if ubi_connect_id is not None:
            data["ubiConnectId"] = ubi_connect_id
        hello = parse_json(await self.request("POST", self.jdns + "hello", data=data))
        self.profile = hello["dancercard"]
        if ubi_connect_id is not None and self.profile.get("ubiConnectIdMapped") is not True:
            raise GameConnectionError("JDNow did not return a dancer profile linked to this Ubisoft account.",
                                      should_reconnect=False)
        player_id = self.profile["_id"]
        if not isinstance(player_id, str) or not IDENTIFIER.fullmatch(player_id):
            raise ValueError("Invalid JDN guest identifier")
        if not isinstance(self.profile.get("publicID"), str) or not IDENTIFIER.fullmatch(self.profile["publicID"]):
            raise ValueError("Invalid JDN public identifier")
        self.auth_token = hello["authToken"]
        if not isinstance(self.auth_token, str) or not 1 <= len(self.auth_token) <= 8192 or "\n" in self.auth_token or "\r" in self.auth_token:
            raise ValueError("Invalid JDN session token")
        self.api = service_url(hello["api"])
        if self.on_guest is not None:
            self.on_guest(player_id, self.profile.get("pName"))
        self.player_id = player_id
        return hello

    async def room_endpoint(self, room):
        for _ in range(3):
            result = parse_json(await self.request("GET", self.jdns + "checkRoomController",
                                                  params={"roomID": int(room)}, accepted=(200, 301)))
            if result.get("jdns"):
                self.jdns = service_url(result["jdns"]).rstrip("/") + "/"
                continue
            host, port = result.get("drs"), result.get("port")
            if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9-]+\.justdancenow\.com", host):
                raise GameConnectionError("JDN could not find this Dance Room.", PairingState.ERROR_INVALID_PAIRING_CODE,
                                          should_reconnect=False)
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid JDN room endpoint")
            return host, port
        raise ValueError("Too many JDN room redirects")

    async def songs(self):
        entries = parse_json(await self.request("GET", self.api + "songs/published", headers={"x-ss-token": APP_IDENTIFIER}))
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValueError("Invalid JDN song catalog")
        return {entry["id"]: entry for entry in entries if isinstance(entry, dict)
                and isinstance(entry.get("id"), str) and ASSET_NAME.fullmatch(entry["id"])}

    async def categories(self):
        entries = parse_json(await self.request("GET", self.jdns + "getCategories",
                                               authenticated=True, params={"lang": "en"}))
        if not isinstance(entries, list) or len(entries) > 1000:
            raise ValueError("Invalid JDN playlists")
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not ASSET_NAME.fullmatch(entry["id"])
                    or not isinstance(entry.get("trackingId", ""), str)
                    or not isinstance(entry.get("songs"), list) or len(entry["songs"]) > 10000
                    or any(not isinstance(song, str) or not ASSET_NAME.fullmatch(song) for song in entry["songs"])):
                raise ValueError("Invalid JDN playlist")
        return entries

    async def rename_player(self, name):
        if not valid_player_name(name):
            raise ValueError(f"Player name must contain 1–{PLAYER_NAME_LIMIT} characters without control characters.")
        result = parse_json(await self.request("POST", self.jdns + "updateDancercard", authenticated=True,
                                               json={"_id": self.player_id, "pName": name.strip()}, limit=1024 * 1024))
        updated = result.get("updated") if isinstance(result, dict) else None
        name = updated.get("pName") if isinstance(updated, dict) else None
        if not valid_player_name(name):
            raise ValueError("JDN did not confirm the new player name.")
        relays = result.get("passToDRS", [])
        if (not isinstance(relays, list) or len(relays) > 8
                or any(not isinstance(item, dict) or set(item) != {"func", "player"}
                       or item["func"] != "updateDancercard" or not isinstance(item["player"], dict) for item in relays)):
            raise ValueError("Invalid JDN profile update notification.")
        self.profile["pName"] = name
        # The room screen redraws the profile before merging the update into its cached player.
        for relay in relays:
            for field in ("fullBodyAvatar", "avatar", "country", "ftueDatas"):
                if self.profile.get(field) is not None:
                    relay["player"].setdefault(field, self.profile[field])
        return relays

    async def asset(self, song, path, *, limit=1024 * 1024):
        url = service_url(song["base"] + "/data/" + path)
        # The location service redirects song data to the regional CDN.
        for _ in range(4):
            try:
                async with self.session.get(url, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = service_url(urljoin(url, response.headers["Location"]))
                        continue
                    if response.status != 200:
                        raise GameConnectionError(f"JDN song data request failed (HTTP {response.status}).",
                                                  should_reconnect=False)
                    return await read_limited(response, limit)
            except (aiohttp.ClientError, TimeoutError) as error:
                raise GameConnectionError("Could not download JDN song data.") from error
        raise ValueError("Too many JDN song redirects")
