from abc import ABC


class AbstractControllerWrapper(ABC):
    """
    Abstract base class for controller wrappers.
    Defines the interface for controller input, rumble, accelerometer, and joystick handling.
    """

    # Set Vendor ID and Product ID
    VENDOR_IDS = {}
    PRODUCT_IDS = {}

    def __init__(self):
        self._available_shortcuts = set()
        self.rumble_enabled = False

    @property
    def available_shortcuts(self):
        """Get or set the available shortcuts for the controller."""
        return self._available_shortcuts

    @available_shortcuts.setter
    def available_shortcuts(self, shortcuts):
        self._available_shortcuts = shortcuts

    async def battery_level(self):
        """Return the battery level of the controller."""
        raise NotImplementedError

    def get_latest_command(self, commands_to_check: set = None):
        """Return the latest command from the controller (button press, etc)."""
        raise NotImplementedError

    def get_accel_events(self):
        """Return accelerometer events from the controller. Returns a list of [x, y, z]."""
        raise NotImplementedError

    def get_joystick_status(self):
        """Return joystick status - dict with analog stick positions."""
        raise NotImplementedError

    def get_joystick_command(self):
        """Return joystick command - Parsed status as a Command value."""
        raise NotImplementedError

    def set_rumble(self, rumble_enabled):
        """Enable or disable rumble on the controller."""
        raise NotImplementedError

    async def send_rumble_data(self, rumble_data):
        """Send rumble command to the controller (async)."""
        raise NotImplementedError

    async def handle_rumble_on_sound_index(self, sound_index):
        """Handle rumble patterns based on sound index (async)."""
        raise NotImplementedError

    async def set_player_led(self, player_id):
        """Sets the player LED to the corresponding pattern.

        Args:
            player_id (int): The player ID, starting from 1.
        """
        raise NotImplementedError
