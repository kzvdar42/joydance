from configparser import ConfigParser
import re
import os
import socket
import sys

from .constants import PairingMethod


REGEX_PAIRING_CODE = re.compile(r'^\d{6}$')
REGEX_LOCAL_IP_ADDRESS = re.compile(r'^(192\.168|10.(\d{1,2}|1\d\d|2[0-4]\d|25[0-5]))\.((\d{1,2}|1\d\d|2[0-4]\d|25[0-5])\.)(\d{1,2}|1\d\d|2[0-4]\d|25[0-5])$')


class ConfigHandler:
    """Config file handler."""

    DEFAULT_CONFIG = {
        'pairing_method': 'default',
        'host_ip_addr': '',
        'console_ip_addr': '',
        'pairing_code': '',
    }

    def __init__(self, cfg_paths: list[str] | str):
        if isinstance(cfg_paths, str):
            cfg_paths = [cfg_paths]
        self.cfg_paths = cfg_paths
        self.current_cfg_path = None
        self._parser = ConfigParser()
        self._data = self.read_data()

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, new_data):
        self._data = self.validate_new_config(new_data)
        self.save_data()

    @staticmethod
    def is_new_config_valid(new_config) -> bool:
        if not is_valid_pairing_method(new_config['pairing_method']):
            return False

        if new_config['pairing_method'] == PairingMethod.DEFAULT.value:
            if (
                not is_valid_ip_address(new_config['host_ip_addr'])
                or not is_valid_pairing_code(new_config['pairing_code'])
            ):
                return False

        if (
            new_config['pairing_method'] == PairingMethod.FAST.value
            and not is_valid_ip_address(new_config['console_ip_addr'])
        ):
            return False
        return True

    @staticmethod
    def validate_new_config(new_config):
        validated_config = ConfigHandler.DEFAULT_CONFIG.copy()
        for key in validated_config:
            if key in new_config:
                val = new_config[key]
                if key == 'pairing_method':
                    if not is_valid_pairing_method(val):
                        val = PairingMethod.DEFAULT.value
                elif key == 'host_ip_addr' or key == 'console_ip_addr':
                    if not is_valid_ip_address(val):
                        val = ''
                elif key == 'pairing_code':
                    if not is_valid_pairing_code(val):
                        val = ''
                elif key.startswith('accel_'):
                    try:
                        val = int(val)
                    except:
                        val = new_config[key]
                validated_config[key] = val
        return validated_config

    def read_data(self):
        self.current_cfg_path = None
        files_read = self._parser.read(filenames=self.cfg_paths)
        # Save the first successful path
        if files_read:
            self.current_cfg_path = files_read[0]
        if 'joydance' not in self._parser:
            self._parser['joydance'] = self.DEFAULT_CONFIG
        else:
            self._parser['joydance'] = self.validate_new_config(
                self._parser['joydance']
            )

        if not self._parser['joydance']['host_ip_addr']:
            host_ip_addr = get_host_ip()
            if host_ip_addr:
                self._parser['joydance']['host_ip_addr'] = host_ip_addr
        return dict(self._parser['joydance'])

    def save_data(self):
        self.current_cfg_path = None
        self._parser['joydance'] = self._data
        for config_path in self.cfg_paths:
            config_folder = os.path.dirname(config_path)
            try:
                if config_folder:
                    os.makedirs(config_folder, exist_ok=True)
                with open(config_path, 'w') as fp:
                    self._parser.write(fp)
                self.current_cfg_path = config_path
                # save to first path with access
                return
            except OSError:
                pass
        else:
            print(f'Failed to write config file to "{self.cfg_paths}"')


def is_valid_pairing_code(val: str) -> bool:
    return re.match(REGEX_PAIRING_CODE, val) is not None


def is_valid_ip_address(val: str) -> bool:
    return re.match(REGEX_LOCAL_IP_ADDRESS, val) is not None


def is_valid_pairing_method(val: str) -> bool:
    return val in {
        PairingMethod.DEFAULT.value,
        PairingMethod.FAST.value,
        PairingMethod.STADIA.value,
        PairingMethod.OLD.value,
    }


def get_host_ip() -> str | None:
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip.startswith('192.168') or ip.startswith('10.'):
                return ip
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
    home = os.path.expanduser('~')
    app_folder = 'JoyDance'

    if sys.platform == 'win32':
        return os.path.join(home, 'AppData', 'Roaming', app_folder)
    elif sys.platform == 'linux':
        return os.path.join(home, '.local', 'share', app_folder)
    elif sys.platform == 'darwin':
        return os.path.join(home, 'Library', 'Application Support', app_folder)
