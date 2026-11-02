"""JD23+ controller authentication reconstructed from controller 27.0.0.61."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from urllib.parse import urlsplit
import uuid

import aiohttp

from joydance.constants import JD_SERVICE_TIMEOUT, PairingState
from joydance.utils import GameConnectionError

APP_ID = "9a22c1f6-1037-4ab6-a536-7cffc3ee4c9c"
BUILD_ID = "main_27.0.0-418218_Android_Final_logical-chain_260923"
SDK_USER_AGENT = "UbiServices_SDK_Unity_2024.Release.18_ANDROID64_dynamic"
UBI_URL = "https://public-ubiservices.ubi.com"


# Application constants shipped by the controller SDK, not player credentials.
SDK_APPLICATION_ID = '1f1b5265-6b99-4127-bad1-eda91fc1944b'
SDK_SIGNING_KEY = bytes.fromhex('4960c61394ff4fd7a730dcc5d57385dc09adfbe22a6ba2b6d8ebc47526fbd86d86fbada81faa7822c0bb461abad71d23bda5536582f106230495f67496b0bfb6')
# The serialized Build Config overrides the native SDK's fallback key and key ID.
EFDS_BUILD_KEY = bytes.fromhex('c5adcbdd4b06573d68d7a15c2333231143530a7b7cf4cf76d5857c830c1cc585')

def mobile_token(user_id, unix_seconds, jitter):
    stamp = datetime.fromtimestamp(unix_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%MZ").encode()
    key = bytes(value ^ stamp[index % len(stamp)] for index, value in enumerate(SDK_SIGNING_KEY))
    signature = hmac.digest(key, uuid.UUID(user_id).bytes_le, "sha256")
    alphabet = "abcdefghkmyzABCEFGHJ0123456789KLMNPRSTUVnpqrstuvwxWXYZ"
    value = (unix_seconds + jitter - 1443657600) * 10
    digits = ""
    while value >= 1 and len(digits) < 10:
        digits = alphabet[int(value % len(alphabet))] + digits
        value /= len(alphabet)
    encode = lambda data: base64.b64encode(data).decode("ascii")
    token = ":".join((user_id, encode(signature), SDK_APPLICATION_ID,
                      encode(("Guest_" + digits).encode()), encode(stamp)))
    return "UbiMobile_v2 t=" + encode(token.encode())


def efds_ticket(ticket, random8, unix_seconds):
    if len(random8) != 8:
        raise ValueError("EFDS requires an eight-byte random nonce.")
    stamp = struct.pack(">q", unix_seconds)
    nonce = random8 + bytes(a ^ ((b << 3) & 255) for a, b in zip(stamp, random8))
    key = hashlib.sha256(hashlib.sha256(EFDS_BUILD_KEY).digest() + nonce).digest()
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 255
        state[i], state[j] = state[j], state[i]
    i = j = 0
    ciphertext = bytearray()
    for value in ("Ubi_v1 " + ticket).encode():
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        ciphertext.append(value ^ state[(state[i] + state[j]) & 255])
    # IL2CPP array initializers are raw bytes; the dump's decoded integer defaults are incorrect here.
    blob = bytearray(bytes.fromhex("c5971c6ef9b18dda")
                     + struct.pack(">I", len(ciphertext)) + ciphertext + nonce)
    if 8 < len(blob) - 69:
        for index in range(len(blob) - 69):
            blob[index] ^= (blob[index + 69] << 5) & 255
    return "Efds_v1 " + base64.b64encode(blob).decode("ascii")


def server_info_url(data):
    # SDK parameter groups carry their JSON fields separately from group metadata.
    groups = data.get("parameters", data.get("parameterGroups", []))
    if isinstance(groups, dict):
        group = groups.get("ServerInfo", {})
    elif isinstance(groups, list):
        group = next((item for item in groups if isinstance(item, dict)
                      and item.get("name", item.get("groupName")) == "ServerInfo"), {})
    else:
        group = {}
    if not isinstance(group, dict):
        raise ValueError("Invalid JD23+ ServerInfo parameter group.")
    fields = group.get("fields", group)
    url = fields.get("url") if isinstance(fields, dict) else None
    if not isinstance(url, str):
        raise ValueError("Ubisoft did not supply the JD23+ ServerInfo URL.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not any(parsed.hostname.endswith("." + domain) or parsed.hostname == domain
                       for domain in ("ubi.com", "ubisoft.com", "just-dance.com"))):
        raise ValueError("Ubisoft supplied an invalid JD23+ service URL.")
    return url.rstrip("/")


class JD23Pairing:
    def __init__(self):
        # Keep a random guest identity for this connection's retries, never derive it from a public device ID.
        self.user_id = str(uuid.uuid4())

    async def _request(self, session, method, url, *, stage, code_request=False, **kwargs):
        async with session.request(method, url, allow_redirects=False, **kwargs) as response:
            if not 200 <= response.status < 300:
                service_error = ""
                try:
                    # Only a numeric code is safe to report: messages may echo credentials or pairing codes.
                    error = json.loads(await response.content.read(4096))
                    code = error.get("errorCode") if isinstance(error, dict) else None
                    if type(code) is int and 0 <= code <= 999999:
                        service_error = f", service error {code}"
                except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                invalid_code = code_request and response.status in {400, 404, 410}
                raise GameConnectionError(
                    "Invalid or expired JD23+ pairing code." if invalid_code else
                    f"JD23+ {stage} failed (HTTP {response.status}{service_error}).",
                    PairingState.ERROR_INVALID_PAIRING_CODE if invalid_code else PairingState.ERROR_CONNECTION,
                    should_reconnect=response.status >= 500 or response.status in {408, 429},
                )
            data = await response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid JD23+ service response.")
            return data

    async def _assign_environment(self, session, space_id, headers, populations):
        data = await self._request(session, "PUT", UBI_URL + "/v1/profiles/me/populations/data",
                                   stage="Ubisoft environment assignment", headers=headers,
                                   json={"spaceId": space_id, "data": {"environment": BUILD_ID}})
        assignments = data.get("populations")
        if (not isinstance(assignments, list) or not 1 <= len(assignments) <= 32
                or any(not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item[key])
                    for key in ("subject", "name")) for item in assignments)):
            raise ValueError("Invalid Ubisoft population assignment response.")
        populations.update((item["subject"], item["name"]) for item in assignments)
        headers["Ubi-Populations"] = ",".join(subject + "." + name for subject, name in populations.items())

    async def pair(self, code, on_pairing):
        # Ubisoft rejects this app with a generic HTTP User-Agent (403 / service error 4000).
        headers = {"Ubi-AppId": APP_ID, "Content-Type": "application/json", "Ubi-localeCode": "en-US",
                   "User-Agent": SDK_USER_AGENT}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
            stamp = int(time.time())
            auth = await self._request(session, "POST", UBI_URL + "/v3/profiles/sessions", stage="Ubisoft login", json={}, headers={
                "Authorization": mobile_token(self.user_id, stamp, secrets.randbelow(100) * 31556926),
                "Ubi-RequestedPlatformType": "ubimobile",
            })
            ticket = auth.get("ticket")
            if not isinstance(ticket, str) or not ticket or len(ticket) > 16384:
                raise ValueError("Invalid Ubisoft ticket response.")
            session_id = str(uuid.UUID(auth["sessionId"]))
            space_id = str(uuid.UUID(auth["spaceId"]))
            ubi_headers = {"Authorization": "Ubi_v1 t=" + ticket, "Ubi-SessionId": session_id}
            populations = {}
            await self._assign_environment(session, space_id, ubi_headers, populations)
            application = await self._request(session, "GET", UBI_URL + f"/v2/applications/{APP_ID}/parameters",
                                              stage="Ubisoft game-space lookup",
                                              params={"parameterGroups": "us-staging"}, headers=ubi_headers)
            game_space_id = str(uuid.UUID(application["parameters"]["us-staging"]["fields"]["spaceId"]))
            if game_space_id != space_id:
                await self._assign_environment(session, game_space_id, ubi_headers, populations)
            parameters = await self._request(session, "GET", UBI_URL + f"/v2/spaces/{game_space_id}/parameters",
                                             stage="Ubisoft ServerInfo lookup", params={"parameterGroups": "ServerInfo"}, headers=ubi_headers)
            base_url = server_info_url(parameters) + "/session/v2"
            efds_headers = {"Authorization": efds_ticket(ticket, secrets.token_bytes(8), int(time.time())),
                            "Ubi-SessionId": session_id, "X-JD-BuildId": BUILD_ID,
                            "X-JD-SkuId": "jdnext-android-all", "Accept-Language": "en-US"}
            await self._request(session, "POST", base_url + "/session", stage="EFDS session creation", headers=efds_headers, json={
                "bootId": str(uuid.uuid4()), "userOwnership": {
                    "dlcCheckToken": None, "subscriptionCheckToken": None,
                    "otherPlayersOnDevice": [], "requestedActivation": [], "deviceId": self.user_id,
                },
            })
            await on_pairing()
            return await self._request(session, "GET", base_url + "/pairing-info", headers=efds_headers,
                                       stage="pairing-code lookup", params={"code": code}, code_request=True)
