import asyncio
from .games.justdance_v1 import JustDanceGameV1
from .games.justdance_v2 import JustDanceGameV2
from .constants import WsSubprotocolVersion


class JoyDance:
    def __init__(
        self,
        controller,
        protocol_version,
        pairing_code=None,
        host_ip_addr=None,
        console_ip_addr=None,
        accel_acquisition_freq_hz=None,
        accel_acquisition_latency=None,
        accel_max_range=None,
        on_state_changed=None,
        on_game_message=None,
    ):
        self.controller = controller
        self.protocol_version = protocol_version

        self._on_state_changed_callback = (
            on_state_changed if on_state_changed else self._default_on_state_changed
        )
        self._on_game_message_callback = (
            on_game_message if on_game_message else self._default_on_game_message
        )

        game_class = None
        if self.protocol_version == WsSubprotocolVersion.V1:
            game_class = JustDanceGameV1
        elif self.protocol_version == WsSubprotocolVersion.V2:
            game_class = JustDanceGameV2
        else:
            raise ValueError(f"Unsupported protocol version: {self.protocol_version}")

        self.game_handler = game_class(
            controller=self.controller,
            pairing_code=pairing_code,
            host_ip_addr=host_ip_addr,
            console_ip_addr=console_ip_addr,
            accel_acquisition_freq_hz=accel_acquisition_freq_hz,
            accel_acquisition_latency=accel_acquisition_latency,
            accel_max_range=accel_max_range,
            on_state_changed=self._on_state_changed_callback,
            on_game_message=self._on_game_message_callback,
        )

    async def pair(self):
        """Initiates the pairing process with the game."""
        await self.game_handler.pair()

    async def disconnect(self, should_reconnect = True):
        """Disconnects from the game."""
        await self.game_handler.disconnect(should_reconnect)

    async def stop_reconnection(self):
        """Stops any ongoing reconnection attempts."""
        await self.game_handler.stop_reconnection()

    def get_profile_data(self):
        """Returns the current player profile data from the game."""
        return self.game_handler.profile_data

    def is_connected_to_game(self):
        """Checks if currently connected to the game."""
        return not self.game_handler.is_connected

    def set_rumble(self, rumble_enabled: bool):
        """Enables or disables rumble on the controller."""
        if hasattr(self.controller, "set_rumble"):
            self.controller.set_rumble(rumble_enabled)
        # Fallback if set_rumble is not present but property is
        elif hasattr(self.controller, "rumble_enabled"):
            self.controller.rumble_enabled = rumble_enabled

    def get_controller_serial(self):
        """Gets the serial number of the controller."""
        if hasattr(self.controller, "serial"):
            return self.controller.serial
        return None

    @staticmethod
    async def _default_on_state_changed(serial, state_data):
        pass

    @staticmethod
    async def _default_on_game_message(message):
        pass
