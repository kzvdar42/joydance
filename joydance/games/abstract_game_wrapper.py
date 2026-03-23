from abc import ABC
import random
import socket
import logging

from joydance.constants import PairingState

logger = logging.getLogger(__name__)


class AbstractGameWrapper(ABC):

    def __init__(
        self,
        on_state_changed=None,
        on_game_message=None,
    ):
        if on_state_changed:
            self._on_state_changed = on_state_changed
        if on_game_message:
            self._on_game_message = on_game_message
        self._state = {}
        self.host_port = self.get_random_port()
        self.console_conn = None
        self.reconnection_start_retry_delay = 1  # seconds
        self.reconnection_max_retry_delay = 30  # seconds

    async def on_state_changed(self, serial, state):  # pylint: disable=method-hidden
        self._state.update(state)
        await self._on_state_changed(serial, self._state)

    async def on_game_message(self, message):  # pylint: disable=method-hidden
        await self._on_game_message(message)

    async def get_state(self, pull_new_data=True):
        if pull_new_data or len(self._state) == 0:
            self._state.update(await self.controller.get_state(pull_new_data))
        return self._state.copy()

    def set_rumble(self, enabled):
        self.controller.set_rumble(enabled)

    def get_random_port(self):
        """Randomize a port number, to be used in hole_punching() later"""
        return random.randrange(39000, 39999)

    async def hole_punching(self):
        """Open a port on this machine so the console can connect to it."""
        try:
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn.settimeout(10)
            conn.bind(("0.0.0.0", self.host_port))
            conn.listen(5)

            # Accept incoming connection from console
            console_conn, addr = conn.accept()
            self.console_conn = console_conn
            logger.debug("Accepted connection from %s:%s", addr[0], addr[1])
        except Exception as e:
            await self.on_state_changed({"state": PairingState.ERROR_HOLE_PUNCHING.value})
            raise e
