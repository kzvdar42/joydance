from configparser import ConfigParser
import re
import os
import socket
import sys
import ipaddress
import json
import tempfile
from uuid import uuid4
from urllib.parse import urlparse

from .constants import PairingMethod
from .utils.network import is_private_ipv4, normalize_console_url


REGEX_PAIRING_CODE = re.compile(r"^\d{6}$")


class ConfigHandler:
    """Config file handler."""

    DEFAULT_CONFIG = {
        "pairing_method": "default",
        "host_ip_addr": "",
        "console_ip_addr": "",
        "pairing_code": "",
    }

    def __init__(self, cfg_paths: list[str] | str):
        if isinstance(cfg_paths, str):
            cfg_paths = [cfg_paths]
        self.cfg_paths = cfg_paths
        self.current_cfg_path = None
        self._parser = ConfigParser(interpolation=None)
        self._data = self.read_data()
        self._connections = []
        stored = self._parser.get("connections", "items", fallback=None)
        if stored is None:
            try:
                self._connections.append(self._validate_connection({**self._data, "name": "Previous connection"}))
            except ValueError:
                pass
        else:
            try:
                entries = json.loads(stored)
                if not isinstance(entries, list):
                    raise ValueError("Expected a list of saved connections")
                self._connections = [self._validate_connection(entry) for entry in entries]
                if len({item["id"] for item in self._connections}) != len(self._connections):
                    raise ValueError("Duplicate saved connection identifiers")
            except (ValueError, TypeError) as error:
                raise ValueError(f"Could not load saved connections from {self.current_cfg_path}: {error}") from error

    @property
    def connections(self):
        return [connection.copy() for connection in self._connections]

    def _validate_connection(self, data):
        if not isinstance(data, dict):
            raise ValueError("Invalid connection settings.")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Give the connection a name of up to 80 characters.")
        if any(not isinstance(data.get(key, ""), str) for key in self.DEFAULT_CONFIG):
            raise ValueError("Invalid connection settings.")
        valid, error = self.validate_config_with_error(data)
        if not valid:
            raise ValueError(error)
        connection = {**self.validate_new_config(data), "id": data.get("id", uuid4().hex), "name": name.strip()}
        if not isinstance(connection["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", connection["id"]):
            raise ValueError("Invalid connection identifier.")
        if data.get("console_url") is not None:
            url = normalize_console_url(data["console_url"])
            if connection["pairing_method"] != PairingMethod.OLD.value or urlparse(url).hostname != connection["console_ip_addr"]:
                raise ValueError("Discovered address does not match the connection settings.")
            connection["console_url"] = url
        return connection

    def save_connection(self, data):
        connection = self._validate_connection(data)
        if data.get("id") and not any(item["id"] == connection["id"] for item in self._connections):
            raise KeyError("This saved connection no longer exists.")
        previous = self._connections
        self._connections = [connection if item["id"] == connection["id"] else item for item in previous]
        if not data.get("id"):
            self._connections.append(connection)
        if not self.save_data():
            self._connections = previous
            raise OSError("Could not save the connection. Check access to the configuration file.")
        return connection.copy()

    def delete_connection(self, identifier):
        previous = self._connections
        self._connections = [item for item in previous if item["id"] != identifier]
        if len(previous) == len(self._connections):
            raise KeyError("This saved connection no longer exists.")
        if not self.save_data():
            self._connections = previous
            raise OSError("Could not delete the connection. Check access to the configuration file.")

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, new_data):
        self._data = self.validate_new_config(new_data)
        self.save_data()

    @staticmethod
    def is_new_config_valid(new_config) -> bool:
        return ConfigHandler.validate_config_with_error(new_config)[0]

    @staticmethod
    def validate_config_with_error(new_config) -> tuple[bool, str]:
        """Validate config and return (is_valid, error_message)."""

        method = new_config.get("pairing_method")
        if not is_valid_pairing_method(method):
            return False, f"Invalid pairing method: {method}"

        if method in {PairingMethod.DEFAULT.value, PairingMethod.STADIA.value, PairingMethod.JDMO.value}:
            if not is_valid_ip_address(new_config.get("host_ip_addr", "")):
                return False, f"Invalid host IP address: {new_config.get('host_ip_addr', '')}"
            if not is_valid_pairing_code(new_config.get("pairing_code", "")):
                return False, f"Invalid pairing code (must be 6 digits): {new_config.get('pairing_code', '')}"

        if method in {PairingMethod.FAST.value, PairingMethod.OLD.value}:
            if not is_valid_ip_address(new_config.get("console_ip_addr", "")):
                return False, f"Invalid console IP address: {new_config.get('console_ip_addr', '')}"

        return True, ""

    @staticmethod
    def validate_new_config(new_config):
        validated_config = ConfigHandler.DEFAULT_CONFIG.copy()
        for key in validated_config:
            if key in new_config:
                val = new_config[key]
                if key == "pairing_method":
                    if not is_valid_pairing_method(val):
                        val = PairingMethod.DEFAULT.value
                elif key == "host_ip_addr" or key == "console_ip_addr":
                    if not is_valid_ip_address(val):
                        val = ""
                elif key == "pairing_code":
                    if not is_valid_pairing_code(val):
                        val = ""
                validated_config[key] = val
        return validated_config

    def read_data(self):
        self.current_cfg_path = None
        files_read = self._parser.read(filenames=self.cfg_paths)
        # Save the first successful path
        if files_read:
            self.current_cfg_path = files_read[0]
        if "joydance" not in self._parser:
            self._parser["joydance"] = self.DEFAULT_CONFIG
        else:
            self._parser["joydance"] = self.validate_new_config(self._parser["joydance"])

        if not self._parser["joydance"]["host_ip_addr"]:
            host_ip_addr = get_host_ip()
            if host_ip_addr:
                self._parser["joydance"]["host_ip_addr"] = host_ip_addr
        return dict(self._parser["joydance"])

    def save_data(self):
        self.current_cfg_path = None
        self._parser["joydance"] = self._data
        self._parser["connections"] = {"items": json.dumps(self._connections)}
        for config_path in self.cfg_paths:
            config_folder = os.path.dirname(config_path)
            temporary_path = None
            try:
                if config_folder:
                    os.makedirs(config_folder, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config_folder or ".", delete=False) as fp:
                    temporary_path = fp.name
                    self._parser.write(fp)
                os.replace(temporary_path, config_path)
                self.current_cfg_path = config_path
                # save to first path with access
                return True
            except OSError:
                pass
            finally:
                if temporary_path and os.path.exists(temporary_path):
                    os.unlink(temporary_path)
        else:
            print(f'Failed to write config file to "{self.cfg_paths}"')
            return False


def is_valid_pairing_code(val: str) -> bool:
    return re.match(REGEX_PAIRING_CODE, val) is not None


def is_valid_ip_address(val: str) -> bool:
    """Validate IP address. Accepts all valid IPv4 addresses (private and public)."""
    if not val:
        return False
    try:
        ipaddress.ip_address(val)
        # Accept both IPv4 and IPv6
        return True
    except ValueError:
        return False


def is_valid_pairing_method(val: str) -> bool:
    return val in {
        PairingMethod.DEFAULT.value,
        PairingMethod.FAST.value,
        PairingMethod.STADIA.value,
        PairingMethod.OLD.value,
        PairingMethod.JDMO.value,
    }


def get_host_ip() -> str | None:
    """Get the host IP address, preferring private network addresses."""
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip_str in addresses:
            if is_private_ipv4(ip_str):
                return ip_str
        # If no private IP found, return the first valid IP
        for ip_str in addresses:
            if is_valid_ip_address(ip_str):
                return ip_str
    except Exception:
        pass
    return None


def get_datadir() -> str:
    """Returns a parent directory path
    where persistent application data can be stored.

    # linux: ~/.local/share
    # macOS: ~/Library/Application Support
    # windows: C:/Users/<USER>/AppData/Roaming
    """
    home = os.path.expanduser("~")
    app_folder = "JoyDance"

    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Roaming", app_folder)
    if sys.platform == "linux":
        return os.path.join(home, ".local", "share", app_folder)
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", app_folder)
    return os.path.join(home, app_folder)
