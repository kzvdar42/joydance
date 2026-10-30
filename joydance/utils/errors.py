
from joydance.constants import PairingState


class ControllerConnectionError(Exception):
    """Raised when a controller HID open or setup fails within bounded attempts."""


class GameConnectionError(Exception):
    """A game connection failure with a browser status and retry policy."""

    def __init__(self, message, state=PairingState.ERROR_CONSOLE_CONNECTION, should_reconnect=True):
        super().__init__(message)
        self.state = state
        self.should_reconnect = should_reconnect
