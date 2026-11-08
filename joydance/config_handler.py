from configparser import ConfigParser
import re
import os
import socket
import sys
import ipaddress
import json
import tempfile
from hashlib import sha256
from uuid import uuid4
from urllib.parse import urlparse

from .constants import PairingMethod
from .utils.network import is_private_ipv4, normalize_console_url


REGEX_PAIRING_CODE = re.compile(r"^\d{6}$")
JDNOW_PLAYER_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")
JDNOW_LINKED_OWNER = "@ubisoft"


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
        self.cfg_paths = list(dict.fromkeys(os.path.abspath(path) for path in cfg_paths))
        self._cfg_path = None
        self.current_cfg_path = None
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

    def _read_jdnow_players(self):
        players = json.loads(self._parser.get("jdnow", "players", fallback="{}"))
        if not isinstance(players, dict):
            raise ValueError("Invalid saved Just Dance Now profiles.")
        return players

    @staticmethod
    def _jdnow_player_record(players, serial):
        record = players.get(serial)
        if record is None:
            return {"active": None, "guests": {}}
        if isinstance(record, str):
            record = {"active": record, "guests": {record: ""}}
        if (not isinstance(record, dict) or not isinstance(record.get("guests"), dict)
                or not isinstance(record.get("active"), str) or record["active"] not in record["guests"]
                or any(not JDNOW_PLAYER_ID.fullmatch(player_id) or not isinstance(name, str)
                       for player_id, name in record["guests"].items())):
            raise ValueError("Invalid saved Just Dance Now profile.")
        return record

    def get_jdnow_player_id(self, serial, guest=None):
        players = self._read_jdnow_players()
        if guest is None:
            return self._jdnow_player_record(players, serial)["active"]
        if guest == "new":
            return None
        for player_id, profile in self._jdnow_guest_pool(players).items():
            if profile["key"] == guest:
                return player_id
        raise ValueError("This Just Dance Now guest is no longer available.")

    def _jdnow_guest_pool(self, players):
        guests = {}
        linked = self._jdnow_player_record(players, JDNOW_LINKED_OWNER)["guests"]
        for serial in players:
            for player_id, name in self._jdnow_player_record(players, serial)["guests"].items():
                if player_id not in guests:
                    # A stable picker key survives filtering and keeps the private ID on the server.
                    guests[player_id] = {"key": sha256(player_id.encode("ascii")).hexdigest(),
                                         "name": name, "controllers": []}
                profile = guests[player_id]
                profile["name"] = profile["name"] or name
                if serial != JDNOW_LINKED_OWNER:
                    profile["controllers"].append(serial)
        for index, (player_id, profile) in enumerate(guests.items(), 1):
            label = f"Ubisoft profile {index}" if player_id in linked else f"Guest {index}"
            profile["name"] = f"{label} · {profile['name']}" if profile["name"] else label
        return guests

    def get_jdnow_guest_states(self, serials, in_use):
        players = self._read_jdnow_players()
        guests = {player_id: profile for player_id, profile in self._jdnow_guest_pool(players).items()
                  if player_id not in in_use}
        options = list(guests.values())
        return {serial: {"jdnow_guests": options,
                         "jdnow_guest": guests.get(self._jdnow_player_record(players, serial)["active"], {}).get("key")}
                for serial in serials}

    def save_jdnow_linked_profile(self, player_id, name):
        self.save_jdnow_player_id(JDNOW_LINKED_OWNER, player_id, name)

    def save_jdnow_player_id(self, serial, player_id, name=None):
        if not isinstance(player_id, str) or not JDNOW_PLAYER_ID.fullmatch(player_id):
            raise ValueError("Invalid Just Dance Now profile.")
        players = self._read_jdnow_players()
        record = self._jdnow_player_record(players, serial)
        guests = record["guests"].copy()
        guests[player_id] = name if isinstance(name, str) and name else guests.get(player_id, "")
        updated = {"active": player_id, "guests": guests}
        changed = players.get(serial) != updated
        players[serial] = updated
        if isinstance(name, str) and name:
            for owner in players:
                if owner == serial:
                    continue
                other = self._jdnow_player_record(players, owner)
                if player_id in other["guests"] and other["guests"][player_id] != name:
                    other["guests"][player_id] = name
                    players[owner] = other
                    changed = True
        if not changed:
            return
        previous = dict(self._parser["jdnow"]) if "jdnow" in self._parser else None
        self._parser["jdnow"] = {**(previous or {}), "players": json.dumps(players)}
        if not self.save_data():
            if previous is None:
                self._parser.remove_section("jdnow")
            else:
                self._parser["jdnow"] = previous
            raise OSError("Could not save the Just Dance Now guest profile.")

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

        if method == PairingMethod.JD23.value and not is_valid_pairing_code(new_config.get("pairing_code", ""), method):
            return False, "Invalid JD23+ pairing code (must be 8 letters or digits)."

        if method == PairingMethod.JDNOW.value and not is_valid_pairing_code(new_config.get("pairing_code", ""), method):
            return False, "Invalid Just Dance Now Dance Room number (must be 4 to 9 digits)."

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
                    if not is_valid_pairing_code(val, new_config.get("pairing_method")):
                        val = ""
                    elif new_config.get("pairing_method") == PairingMethod.JD23.value:
                        val = val.upper()
                validated_config[key] = val
        return validated_config

    def read_data(self):
        self.current_cfg_path = None
        self._cfg_path = None
        self._parser = ConfigParser(interpolation=None)
        existing_paths = [path for path in self.cfg_paths if os.path.exists(path)]
        # Reuse an existing config before creating a new one beside the application.
        for path in existing_paths + [path for path in self.cfg_paths if path not in existing_paths]:
            if not self._can_write_config(path):
                continue
            parser = ConfigParser(interpolation=None)
            files_read = parser.read(path, encoding="utf-8")
            if path in existing_paths and not files_read:
                continue
            self._cfg_path = path
            if files_read:
                self._parser = parser
                self.current_cfg_path = path
            break
        if self.current_cfg_path is None:
            # Preserve settings when a read-only local file needs a new fallback.
            for path in existing_paths:
                parser = ConfigParser(interpolation=None)
                if parser.read(path, encoding="utf-8"):
                    self._parser = parser
                    self.current_cfg_path = path
                    break
        if "joydance" not in self._parser:
            self._parser["joydance"] = self.DEFAULT_CONFIG
        else:
            self._parser["joydance"] = self.validate_new_config(self._parser["joydance"])

        if not self._parser["joydance"]["host_ip_addr"]:
            host_ip_addr = get_host_ip()
            if host_ip_addr:
                self._parser["joydance"]["host_ip_addr"] = host_ip_addr
        return dict(self._parser["joydance"])

    @staticmethod
    def _can_write_config(path):
        try:
            if os.path.exists(path) and not os.access(path, os.R_OK | os.W_OK):
                return False
            directory = os.path.dirname(path)
            os.makedirs(directory, exist_ok=True)
            # Atomic replacement also needs permission to create files in the directory.
            with tempfile.TemporaryFile(dir=directory):
                pass
            return True
        except OSError:
            return False

    def save_data(self):
        self.current_cfg_path = None
        self._parser["joydance"] = self._data
        self._parser["connections"] = {"items": json.dumps(self._connections)}
        config_path = self._cfg_path
        if config_path is not None:
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
                return True
            except OSError:
                pass
            finally:
                if temporary_path and os.path.exists(temporary_path):
                    os.unlink(temporary_path)
        print(f'Failed to write config file to "{config_path or self.cfg_paths}"')
        return False


def is_valid_pairing_code(val: str, method=None) -> bool:
    if not isinstance(val, str):
        return False
    if method == PairingMethod.JD23.value:
        return re.fullmatch(r"[a-zA-Z0-9]{8}", val) is not None
    if method == PairingMethod.JDNOW.value:
        return re.fullmatch(r"[0-9]{4,9}", val) is not None
    return re.fullmatch(REGEX_PAIRING_CODE, val) is not None


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
        PairingMethod.JD23.value,
        PairingMethod.JDNOW.value,
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
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(appdata, app_folder)
    if sys.platform == "linux":
        return os.path.join(home, ".local", "share", app_folder)
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", app_folder)
    return os.path.join(home, app_folder)


def get_config_paths(script_path: str) -> list[str]:
    application_path = sys.executable if getattr(sys, "frozen", False) else script_path
    local_path = os.path.join(os.path.dirname(os.path.abspath(application_path)), "config.cfg")
    return [local_path, os.path.join(get_datadir(), "config.cfg")]
