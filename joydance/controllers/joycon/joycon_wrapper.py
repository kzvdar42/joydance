import asyncio
import logging

from .pycon import ButtonEventJoyCon
from .pycon.constants import (
    JOYCON_VENDOR_ID, JOYCON_PRODUCT_IDS,
    JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID,
)
from joydance.constants import JoyConButton, Command, BUTTON_MAPPING, PRO_BUTTON_MAPPING
from joydance.controllers import AbstractControllerWrapper


logger = logging.getLogger("joydance")


class JoyConWrapper(AbstractControllerWrapper):

    VENDOR_IDS = {JOYCON_VENDOR_ID}
    PRODUCT_IDS = JOYCON_PRODUCT_IDS

    def __init__(self, vendor_id, product_id, serial):
        name = "Pro Controller" if product_id == JOYCON_PRO_PRODUCT_ID else "JoyCon"
        super().__init__(vendor_id, product_id, serial, name=name)
        self.joycon = ButtonEventJoyCon(vendor_id, product_id, serial)
        self.rumble_enabled = True
        self._is_battery_level_initialized = False
        self._led_transition_task = None
        self._initialize_state()
        self._state["controller_type"] = {
            JOYCON_L_PRODUCT_ID: "joycon_left",
            JOYCON_R_PRODUCT_ID: "joycon_right",
            JOYCON_PRO_PRODUCT_ID: "pro_controller",
        }[product_id]

    async def get_state(self, pull_new_data=True):
        if pull_new_data or "battery_level" not in self._state:
            self._state["battery_level"] = await self.battery_level()
        if "color" not in self._state and self.joycon.was_state_initialized:
            if not self.is_pro():
                self.name = f"JoyCon {('L' if self.is_left() else 'R')}"
            self._state["name"] = self.name
            self._state["color"] = "#%02x%02x%02x" % self.color_body
            self._state["color_buttons"] = "#%02x%02x%02x" % self.joycon.color_btn
            if self.is_pro():
                self._state["color_grip_left"] = "#%02x%02x%02x" % (
                    self.joycon.color_grip_left or self.color_body
                )
                self._state["color_grip_right"] = "#%02x%02x%02x" % (
                    self.joycon.color_grip_right or self.color_body
                )
        return self._state.copy()

    @property
    def color_body(self):
        return self.joycon.color_body

    @property
    def available_shortcuts(self):
        return self._available_shortcuts

    @available_shortcuts.setter
    def available_shortcuts(self, shortcuts_set):
        self._available_shortcuts = shortcuts_set

    async def battery_level(self):
        if self._is_battery_level_initialized:
            return self.joycon.get_battery_level()
        for _ in range(3):
            await asyncio.sleep(0.05)
            battery_level = self.joycon.get_battery_level()
            if battery_level > 0:
                break
        self._is_battery_level_initialized = True
        return battery_level

    def get_raw_button_events(self):
        """Returns raw button events from the JoyCon."""
        return self.joycon.events()

    def is_command_pressed(self, cmd):
        button_mapping = PRO_BUTTON_MAPPING if self.is_pro() else BUTTON_MAPPING
        return self.is_connected() and any(
            cmd in commands and bool(getattr(self.joycon, button.value, 0))
            for button, commands in button_mapping.items()
        )

    def get_latest_command(self, commands_to_check=None):
        available_commands = self.available_shortcuts | {
            Command.ACCEPT, Command.BACK, Command.PAUSE,
        }
        if self.is_pro():
            available_commands |= {Command.UP, Command.RIGHT, Command.DOWN, Command.LEFT}
        button_mapping = PRO_BUTTON_MAPPING if self.is_pro() else BUTTON_MAPPING
        if commands_to_check is not None:
            available_commands &= commands_to_check

        # Get pressed button
        for event_type, status in self.get_raw_button_events():
            if status == 0:  # 0 = released, 1 = pressed
                continue

            try:
                joycon_button = JoyConButton(event_type)
            except ValueError:
                logger.warning("Unknown button event_type: %s in get_latest_command", event_type)
                continue

            for cmd in button_mapping.get(joycon_button, ()):
                if cmd in available_commands:
                    return cmd
        return None

    def set_accel_collection(self, enabled):
        self.joycon.set_accel_collection(enabled)

    def get_accel_events(self):
        try:
            return self.joycon.get_accel_events()
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
                if self.is_pro():
                    stick_sides = ("left", "right")
                else:
                    stick_sides = ("left",) if self.is_left() else ("right",)
                for stick_side in stick_sides:
                    stick = controller_status["analog-sticks"].get(stick_side, {})
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
        self._stop_led_transition()
        self.joycon.close()

    async def connect(self):
        """Connects to the JoyCon without blocking the event loop."""
        return await self.joycon.connect()

    async def reconnect(self):
        """Attempts to reconnect to the JoyCon without blocking the event loop."""
        return await self.joycon.reconnect()

    def is_connected(self):
        """Checks if the JoyCon is currently connected."""
        return self.joycon.is_connected.is_set()

    def is_left(self):
        """Checks if the JoyCon is a left JoyCon."""
        return self.joycon.is_left()

    def is_pro(self):
        return self.product_id == JOYCON_PRO_PRODUCT_ID

    async def handle_rumble_on_sound_index(self, sound_index):
        if not self.rumble_enabled:
            return
        try:
            if sound_index == 0:  # Coach selection
                self.rumble(frequency=160.0, amplitude=0.3)
                await asyncio.sleep(0.15)
            elif 1 <= sound_index <= 5:  # Stars (1-5)
                strength = 0.3 + (sound_index * 0.12)
                duration = 0.1 + (sound_index * 0.05)
                self.rumble(frequency=240.0, amplitude=strength)
                await asyncio.sleep(duration)
            elif sound_index == 6:  # Megastar
                for amp_val in [0.5, 0.7, 1.0]:
                    self.rumble(frequency=320.0, amplitude=amp_val)
                    await asyncio.sleep(0.15)
                    self.stop_rumble()
                    await asyncio.sleep(0.08)
            elif sound_index == 7:  # Star move
                self.rumble(frequency=280.0, amplitude=0.8)
                await asyncio.sleep(0.2)
            elif sound_index == 8:  # Start dance
                self.rumble(frequency=240.0, amplitude=0.6)
                await asyncio.sleep(0.15)
                self.stop_rumble()
                await asyncio.sleep(0.1)
                self.rumble(frequency=320.0, amplitude=0.8)
                await asyncio.sleep(0.2)
        finally:
            self.stop_rumble()

    def _stop_led_transition(self):
        task, self._led_transition_task = self._led_transition_task, None
        if task is not None:
            task.cancel()

    async def _sweep_player_led(self):
        for pattern in (1, 2, 4, 8):
            self.joycon.set_player_lamp(pattern)
            await asyncio.sleep(0.15)

    async def animate_connecting_led(self):
        self._stop_led_transition()
        try:
            while True:
                await self._sweep_player_led()
        except Exception:
            logger.exception("Error animating connecting LEDs on JoyCon.")

    async def _animate_player_led(self, pattern, move_to_unassigned=False):
        try:
            if move_to_unassigned:
                await self._sweep_player_led()
            for _ in range(2):
                await asyncio.sleep(0.35)
                self.joycon.set_player_lamp(0)
                await asyncio.sleep(0.35)
                self.joycon.set_player_lamp(pattern)
        except Exception:
            logger.exception("Error animating player LED transition on JoyCon.")

    async def set_player_led(self, player_id, flash=False):
        """Sets the player LED to the corresponding pattern.

        player_id can be set to 0 for "disconnected" pattern.

        Args:
            player_id (int): The player ID, starting from 1.
            flash (bool): Blink the pattern twice without delaying the caller.
        """
        self._stop_led_transition()
        # better works after battery level is read
        if not self._is_battery_level_initialized:
            await self.battery_level()
        # Using pattern from https://en-americas-support.nintendo.com/app/answers/detail/a_id/22424
        patterns = {
            1: 1,
            2: 3,
            3: 7,
            4: 15,
            5: 9,
            6: 5,
            7: 13,
            8: 6
        }
        lamp_pattern = patterns.get(player_id, 8)
        try:
            move_to_unassigned = flash and player_id == 0
            if not move_to_unassigned:
                self.joycon.set_player_lamp(lamp_pattern)
            if flash:
                self._led_transition_task = asyncio.create_task(
                    self._animate_player_led(lamp_pattern, move_to_unassigned=move_to_unassigned)
                )
        except Exception:
            logger.exception("Error setting player LED on JoyCon.")
