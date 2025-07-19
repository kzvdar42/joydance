import asyncio
import logging

from joydance.constants import JoyConButton, Command, SHORTCUT_MAPPING
from .abstract_controller_wrapper import AbstractControllerWrapper


logger = logging.getLogger("joydance")


class JoyConWrapper(AbstractControllerWrapper):

    def __init__(self, joycon):
        super().__init__()
        self.joycon = joycon
        self.rumble_enabled = True  # Default to True, can be changed by set_rumble
        self._available_shortcuts = set()  # To be updated by game handler

    @property
    def serial(self):
        return self.joycon.serial

    @property
    def available_shortcuts(self):
        return self._available_shortcuts

    @available_shortcuts.setter
    def available_shortcuts(self, shortcuts_set):
        self._available_shortcuts = shortcuts_set

    def get_raw_button_events(self):
        """Returns raw button events from the JoyCon."""
        return self.joycon.events()

    def get_latest_command(self, commands_to_check=None):
        # Get pressed button
        for event_type, status in self.get_raw_button_events():
            if status == 0:  # 0 = pressed, 1 = released
                continue

            try:
                joycon_button = JoyConButton(event_type)
            except ValueError:
                logger.warning(f"Unknown button event_type: {event_type} in get_latest_command")
                continue

            cmds = []
            if joycon_button == JoyConButton.PLUS or joycon_button == JoyConButton.MINUS:
                cmds.append(Command.PAUSE)
            elif joycon_button == JoyConButton.A or joycon_button == JoyConButton.RIGHT:
                cmds.append(Command.ACCEPT)
            elif joycon_button == JoyConButton.B or joycon_button == JoyConButton.DOWN:
                cmds.append(Command.BACK)
            elif joycon_button in SHORTCUT_MAPPING:
                for shortcut in SHORTCUT_MAPPING[joycon_button]:
                    if shortcut in self.available_shortcuts:
                        cmds.append(shortcut)
            if cmds:
                if commands_to_check:
                    for cmd in cmds:
                        if cmd in commands_to_check:
                            return cmd
                else:
                    return cmds[0]
        return None

    def get_accel_events(self):
        try:
            return self.joycon.get_accels()
        except OSError:
            logger.exception("Error reading accelerometer data from JoyCon.")
            return []

    def get_joystick_status(self):
        try:
            return self.joycon.get_status()
        except OSError:
            logger.exception("Error reading joystick status from JoyCon.")
            return {}

    def get_joystick_command(self):
        try:
            controller_status = self.get_joystick_status()
            if controller_status and "analog-sticks" in controller_status:
                stick_side = "left" if self.is_left() else "right"
                if stick_side in controller_status["analog-sticks"]:
                    stick = controller_status["analog-sticks"][stick_side]
                    vertical = stick.get("vertical", 0)
                    horizontal = stick.get("horizontal", 0)
                    if vertical < -0.5:
                        return Command.DOWN
                    elif vertical > 0.5:
                        return Command.UP
                    elif horizontal < -0.5:
                        return Command.LEFT
                    elif horizontal > 0.5:
                        return Command.RIGHT
        except OSError:
            logger.exception("Error reading joystick status from JoyCon.")
            return None

    def set_rumble(self, rumble_enabled):
        self.rumble_enabled = rumble_enabled
        # Assuming the actual joycon object has its own rumble_enabled state or method
        if hasattr(self.joycon, "rumble_enabled"):
            self.joycon.rumble_enabled = rumble_enabled

    def rumble(self, frequency=160.0, amplitude=0.3):
        """Sends a rumble command to the JoyCon."""
        if not self.rumble_enabled:
            return
        self.joycon.rumble(frequency=frequency, amplitude=amplitude)

    def stop_rumble(self):
        """Stops any active rumble on the JoyCon."""
        self.joycon.stop_rumble()

    def close(self):
        """Closes the connection to the JoyCon."""
        self.joycon.close()

    async def reconnect(self):
        """Attempts to reconnect to the JoyCon."""
        try:
            await self.joycon.reconnect()
            return True
        except Exception as e:
            logger.error(f"JoyConWrapper reconnect failed: {e}")
            return False

    def is_connected(self):
        """Checks if the JoyCon is currently connected."""
        return self.joycon.is_connected()

    def is_left(self):
        """Checks if the JoyCon is a left JoyCon."""
        return self.joycon.is_left()

    async def handle_rumble_on_sound_index(self, sound_index):
        if not self.rumble_enabled:
            return
        if sound_index == 0:  # Coach selection
            self.rumble(frequency=160.0, amplitude=0.3)
            await asyncio.sleep(0.15)
            self.stop_rumble()
        elif 1 <= sound_index <= 5:  # Stars (1-5)
            strength = 0.3 + (sound_index * 0.12)
            duration = 0.1 + (sound_index * 0.05)
            self.rumble(frequency=240.0, amplitude=strength)
            await asyncio.sleep(duration)
            self.stop_rumble()
        elif sound_index == 6:  # Megastar
            for amp_val in [0.5, 0.7, 1.0]:
                self.rumble(frequency=320.0, amplitude=amp_val)
                await asyncio.sleep(0.15)
                self.stop_rumble()
                await asyncio.sleep(0.08)
        elif sound_index == 7:  # Star move
            self.rumble(frequency=280.0, amplitude=0.8)
            await asyncio.sleep(0.2)
            self.stop_rumble()
        elif sound_index == 8:  # Start dance
            self.rumble(frequency=240.0, amplitude=0.6)
            await asyncio.sleep(0.15)
            self.stop_rumble()
            await asyncio.sleep(0.1)
            self.rumble(frequency=320.0, amplitude=0.8)
            await asyncio.sleep(0.2)
            self.stop_rumble()
