"""HTTP pairing shared by the V1 and V2 phone protocols."""

import json
from urllib.parse import urlparse

import aiohttp

from joydance.constants import JD_SERVICE_TIMEOUT, PairingMethod, PairingState, UBI_APP_ID, UBI_SKU_ID
from joydance.utils import GameConnectionError

from .justdance_abstract import JustDanceGameAbstract
from .v2 import jdmo_pairing


class LegacyJDGame(JustDanceGameAbstract):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            "Ubi-AppId": UBI_APP_ID,
            "X-SkuId": UBI_SKU_ID,
        }

    async def get_access_token(self):
        headers = {
            "Authorization": "UbiMobile_v1 t=NTNjNWRjZGMtZjA2Yy00MTdmLWJkMjctOTNhZTcxNzU1OTkyOlcwM0N5eGZldlBTeFByK3hSa2hhQ05SMXZtdz06UjNWbGMzUmZaVzB3TjJOYTpNakF5TVMweE1DMHlOMVF3TVRvME5sbz0=",
            "Ubi-AppId": UBI_APP_ID,
            "User-Agent": "UbiServices_SDK_Unity_Light_Mobile_2018.Release.16_ANDROID64_dynamic",
            "Ubi-RequestedPlatformType": "ubimobile",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
            async with session.post(
                "https://public-ubiservices.ubi.com/v1/profiles/sessions",
                json={},
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    raise GameConnectionError(
                        f"Pairing authentication failed (HTTP {resp.status}).",
                        PairingState.ERROR_CONNECTION,
                        should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                    )
                json_body = await resp.json()
                if not isinstance(json_body, dict) or not isinstance(json_body.get("ticket"), str) or not json_body["ticket"]:
                    raise GameConnectionError("Invalid authentication response from the pairing service.", PairingState.ERROR_CONNECTION)
                self.headers["Authorization"] = "Ubi_v1 " + json_body["ticket"]

    async def send_pairing_code(self):
        if self.pairing_method == PairingMethod.JDMO.value:
            json_body = await jdmo_pairing.request_pairing("pairing-info", headers=self.headers, pairing_code=self.pairing_code)
        else:
            url = "https://prod.just-dance.com/sessions/v1/pairing-info"
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
                async with session.get(url, params={"code": self.pairing_code}, ssl=False) as resp:
                    if resp.status != 200:
                        invalid_code = resp.status in {400, 404, 410}
                        raise GameConnectionError(
                            "Invalid or expired pairing code." if invalid_code else f"Pairing service rejected the request (HTTP {resp.status}).",
                            PairingState.ERROR_INVALID_PAIRING_CODE if invalid_code else PairingState.ERROR_CONNECTION,
                            should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                        )
                    json_body = await resp.json()
        if not isinstance(json_body, dict) or not isinstance(json_body.get("pairingUrl"), str):
            raise GameConnectionError("Invalid console address from the pairing service.", PairingState.ERROR_CONNECTION)
        try:
            uri = urlparse(json_body["pairingUrl"])
            if uri.scheme not in {"http", "https", "ws", "wss"} or not uri.hostname or uri.username or uri.password or uri.fragment:
                raise ValueError("Invalid pairing URL")
            uri.port  # Validate the port before passing the address to websockets.
        except ValueError as error:
            raise GameConnectionError("Invalid console address from the pairing service.", PairingState.ERROR_CONNECTION) from error
        path = uri.path.rstrip("/")
        if not path.endswith("/smartphone"):
            path += "/smartphone"
        self.pairing_url = uri._replace(scheme={"http": "ws", "https": "wss"}.get(uri.scheme, uri.scheme), path=path).geturl()
        self.tls_certificate = json_body.get("tlsCertificate")
        self.requires_punch_pairing = json_body.get("requiresPunchPairing", False) in (True, 1, "1", "true")

    async def send_initiate_punch_pairing(self):
        url = "https://prod.just-dance.com/sessions/v1/initiate-punch-pairing"
        json_payload = {
            "pairingCode": self.pairing_code,
            "mobileIP": self.host_ip_addr,
            "mobilePort": self.host_port,
        }

        if self.pairing_method == PairingMethod.JDMO.value:
            await jdmo_pairing.request_pairing(
                "initiate-punch-pairing", headers=self.headers, pairing_code=self.pairing_code, payload=json_payload,
            )
            return

        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=JD_SERVICE_TIMEOUT)) as session:
                async with session.post(url, json=json_payload, ssl=False) as resp:
                    body = await resp.text()
                    if resp.status != 200 or body.strip() != "OK":
                        raise GameConnectionError(
                            f"The pairing service could not request a console connection (HTTP {resp.status}).",
                            PairingState.ERROR_PUNCH_PAIRING,
                            should_reconnect=resp.status >= 500 or resp.status in {408, 429},
                        )
        except (aiohttp.ClientError, TimeoutError) as error:
            raise GameConnectionError(
                f"Could not request the console connection: {str(error) or 'request timed out'}.",
                PairingState.ERROR_PUNCH_PAIRING,
            ) from error
