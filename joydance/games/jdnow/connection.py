"""JDN TCP framing, exposed through JoyDance's connection lifecycle."""
import asyncio
import re
import zlib

from websockets.protocol import State

from joydance.utils import GameConnectionError
from .dictionary import DICTIONARY


MAX_FRAME_SIZE = 0x1FFFF
MAX_MESSAGE_SIZE = 512 * 1024


def encode_frame(message):
    payload = message.encode("utf-8")
    if not 0 < len(payload) <= MAX_MESSAGE_SIZE:
        raise ValueError("Invalid JDN message length.")
    encoder = zlib.compressobj(wbits=-15, memLevel=9, zdict=DICTIONARY)
    payload = encoder.compress(payload) + encoder.flush()
    if not 0 < len(payload) <= MAX_FRAME_SIZE:
        raise ValueError("Invalid JDN compressed message length.")
    size, header = len(payload) | (2 << 17), ""
    while size:
        size, digit = divmod(size, 36)
        header = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + header
    return header.zfill(4).encode("ascii") + payload


def decode_payload(header, payload):
    if not re.fullmatch(rb"[0-9a-zA-Z]{4}", header):
        raise GameConnectionError("Invalid JDN frame header.", should_reconnect=False)
    value = int(header, 36)
    if not 0 < (value & MAX_FRAME_SIZE) == len(payload):
        raise GameConnectionError("Invalid JDN frame length.", should_reconnect=False)
    strategy = value >> 17
    try:
        if strategy == 2:
            decoder = zlib.decompressobj(-15, zdict=DICTIONARY)
            payload = decoder.decompress(payload, MAX_MESSAGE_SIZE + 1)
            if len(payload) > MAX_MESSAGE_SIZE or not decoder.eof or decoder.unused_data:
                raise ValueError("Invalid compressed message")
        elif strategy != 0:
            raise ValueError("Unsupported compression strategy")
        return payload.decode("utf-8")
    except (UnicodeError, zlib.error, ValueError) as error:
        raise GameConnectionError("Invalid JDN message encoding.", should_reconnect=False) from error


class JDNowConnection:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.state = State.OPEN
        self._send_lock = asyncio.Lock()

    async def send(self, message):
        frame = encode_frame(message)
        async with self._send_lock:
            if self.state is not State.OPEN:
                raise GameConnectionError("The JDN connection is closed.")
            self.writer.write(frame)
            await self.writer.drain()

    async def __aiter__(self):
        while self.state is State.OPEN:
            try:
                header = await self.reader.readexactly(4)
            except asyncio.IncompleteReadError as error:
                if error.partial:
                    raise GameConnectionError("Truncated JDN frame header.") from error
                if self.state is State.OPEN:
                    raise GameConnectionError("The JDN room server closed the TCP connection.") from error
                return
            if not re.fullmatch(rb"[0-9a-zA-Z]{4}", header):
                raise GameConnectionError("Invalid JDN frame header.", should_reconnect=False)
            size = int(header, 36) & MAX_FRAME_SIZE
            if not 0 < size <= MAX_FRAME_SIZE:
                raise GameConnectionError("Invalid JDN frame length.", should_reconnect=False)
            try:
                payload = await self.reader.readexactly(size)
            except asyncio.IncompleteReadError as error:
                raise GameConnectionError("Truncated JDN frame.") from error
            yield decode_payload(header, payload)

    async def close(self):
        if self.state is State.CLOSED:
            return
        self.state = State.CLOSED
        self.writer.close()
        try:
            async with asyncio.timeout(3):
                await self.writer.wait_closed()
        except (OSError, TimeoutError):
            pass
