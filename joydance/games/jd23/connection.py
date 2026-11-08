"""The uncompressed Harbour stream used by the JD23+ controller."""
import asyncio
import struct

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import StreamReset
from websockets.protocol import State

from joydance.utils import GameConnectionError


class HarbourProtocol(QuicConnectionProtocol):
    def connect(self, addr, transmit=True):
        # The APK trusts its console CA; certificates do not name the console's LAN IP.
        # Set configuration before TLS starts so a QUIC Retry keeps the same policy.
        self._quic.configuration.server_name = None
        super().connect(addr, transmit=transmit)

    def quic_event_received(self, event):
        if isinstance(event, StreamReset) and event.stream_id in self._stream_readers:
            self._stream_readers[event.stream_id].set_exception(
                GameConnectionError("The game reset the Harbour stream.")
            )
        super().quic_event_received(event)


class HarbourConnection:
    """Expose the connection operations used by JoyDance's existing lifecycle."""

    def __init__(self, protocol, reader, writer):
        self.protocol = protocol
        self.reader = reader
        self.writer = writer
        self.state = State.OPEN
        self._send_lock = asyncio.Lock()

    async def send(self, message):
        payload = message.encode("utf-8") + b"\0"
        if b"\0" in payload[:-1] or not 1 <= len(payload) <= 32768:
            raise ValueError("Invalid Harbour message length or terminator.")
        async with self._send_lock:
            if self.state is not State.OPEN:
                raise GameConnectionError("The Harbour connection is closed.")
            self.writer.write(struct.pack("<H", len(payload)) + payload)
            await self.writer.drain()

    async def __aiter__(self):
        while self.state is State.OPEN:
            try:
                header = await self.reader.readexactly(2)
            except asyncio.IncompleteReadError as error:
                if error.partial:
                    raise GameConnectionError("Truncated Harbour frame header.") from error
                return
            size = struct.unpack("<H", header)[0]
            if not 1 <= size <= 0xC000:
                raise GameConnectionError("Invalid Harbour frame length.")
            try:
                payload = await self.reader.readexactly(size)
            except asyncio.IncompleteReadError as error:
                raise GameConnectionError("Truncated Harbour frame.") from error
            if not payload.endswith(b"\0"):
                raise GameConnectionError("Missing Harbour message terminator.")
            for message in payload[:-1].split(b"\0"):
                if message:
                    yield message.decode("utf-8")

    async def close(self):
        if self.state is State.CLOSED:
            return
        self.state = State.CLOSED
        try:
            self.writer.close()
        finally:
            self.protocol.close()
            await self.protocol.wait_closed()


# Public trust anchor shipped in the official controller; never trust arbitrary LAN peers.
ROOT_CA = b"""-----BEGIN CERTIFICATE-----
MIIDQzCCAqSgAwIBAgIUCNIn9KLeg/3N1CnYZfiM2kGZrgYwCgYIKoZIzj0EAwQw
XDELMAkGA1UEBhMCRlIxEDAOBgNVBAoTB1ViaXNvZnQxEzARBgNVBAsTCkp1c3Qg
RGFuY2UxJjAkBgNVBAMTHUp1c3QgRGFuY2UgVmF1bHQgMjAyNCBSb290IENBMCAX
DTI0MDczMDEzNTI0MFoYDzIwNTQwNzIzMTM1MzEwWjBcMQswCQYDVQQGEwJGUjEQ
MA4GA1UEChMHVWJpc29mdDETMBEGA1UECxMKSnVzdCBEYW5jZTEmMCQGA1UEAxMd
SnVzdCBEYW5jZSBWYXVsdCAyMDI0IFJvb3QgQ0EwgZswEAYHKoZIzj0CAQYFK4EE
ACMDgYYABADwjoLX7qFsFpeJGnlwnB9SXng8ERsxzta52QvSDuGpMEJ5ZLWBAdRh
9NKhnt0I1V103/jT+4eo691fSwYnit3r8ADizQONMfWSHuGyzNTNmAw3qqfjTo/2
Q55bNiKlniqbw1TQr4xumdhY6Rm0J5jBaBz9fqw++e1ZjaTrSjNcibJC0qOB/jCB
+zAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUUDDr
bqgfDKtizQIZjwabi4BKfeQwHwYDVR0jBBgwFoAUUDDrbqgfDKtizQIZjwabi4BK
feQwUAYIKwYBBQUHAQEERDBCMEAGCCsGAQUFBzAChjRodHRwczovL2xpdmUwMS1u
Y3NhLnZhdWx0LnViaXNvZnQuY29tOjgyMDAvdjEvcGtpL2NhMEYGA1UdHwQ/MD0w
O6A5oDeGNWh0dHBzOi8vbGl2ZTAxLW5jc2EudmF1bHQudWJpc29mdC5jb206ODIw
MC92MS9wa2kvY3JsMAoGCCqGSM49BAMEA4GMADCBiAJCAYYqQXwckzM4s+DrJgRC
C3k3wEEj45+nrz4RlEspeyNDjo15hbAky5YYJ/nLydelohEaqZ8PnNTyCm0iauPh
R/3/AkIA2PQj97nbW12Z6GuUx7ceEn6egqHohOCbUKTIdFMqNIrCQfItHpIol+hN
yeMMknirjDDNGISimZaNhSVNddfkbaA=
-----END CERTIFICATE-----
"""
