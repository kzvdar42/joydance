import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from joydance.config_handler import ConfigHandler, get_host_ip, is_valid_pairing_method


class PairingConfigTests(unittest.TestCase):
    def test_both_validation_entry_points_apply_the_same_requirements(self):
        complete = {"host_ip_addr": "192.168.1.2", "console_ip_addr": "192.168.1.3", "pairing_code": "012345"}
        for method in ("default", "fast", "stadia", "old", "jdmo"):
            for changes in ({}, {"host_ip_addr": ""}, {"console_ip_addr": ""}, {"pairing_code": ""}):
                with self.subTest(method=method, changes=changes):
                    config = {**complete, "pairing_method": method, **changes}
                    expected = not (
                        method in {"default", "stadia", "jdmo"} and (not config["host_ip_addr"] or not config["pairing_code"])
                        or method == "fast" and not config["console_ip_addr"]
                    )
                    self.assertEqual(ConfigHandler.validate_config_with_error(config)[0], expected)
                    self.assertEqual(ConfigHandler.is_new_config_valid(config), expected)
        self.assertFalse(ConfigHandler.is_new_config_valid({}))

    def test_jdmo_requires_host_address_and_six_digit_code(self):
        config = {"pairing_method": "jdmo", "host_ip_addr": "192.168.1.2", "pairing_code": "012345"}
        self.assertTrue(is_valid_pairing_method("jdmo"))
        self.assertTrue(ConfigHandler.is_new_config_valid(config))
        self.assertEqual(ConfigHandler.validate_config_with_error(config), (True, ""))
        for changes in ({"pairing_code": "123"}, {"pairing_code": "abcdef"}, {"host_ip_addr": "invalid"}):
            with self.subTest(changes=changes):
                self.assertFalse(ConfigHandler.is_new_config_valid({**config, **changes}))
                self.assertFalse(ConfigHandler.validate_config_with_error({**config, **changes})[0])

    def test_jdmo_selection_survives_config_reload(self):
        with tempfile.TemporaryDirectory() as directory, patch("joydance.config_handler.get_host_ip", return_value="192.168.1.2"):
            path = str(Path(directory) / "config.cfg")
            config = ConfigHandler(path)
            config.data = {"pairing_method": "jdmo", "host_ip_addr": "192.168.1.2", "pairing_code": "012345"}
            reloaded = ConfigHandler(path)
            self.assertEqual(reloaded.data["pairing_method"], "jdmo")
            self.assertEqual(reloaded.data["pairing_code"], "012345")


class HostAddressTests(unittest.TestCase):
    def test_private_lan_addresses_take_priority_over_other_addresses(self):
        for address in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.2"):
            with self.subTest(address=address), patch(
                "joydance.config_handler.socket.gethostbyname_ex",
                return_value=("host", [], ["invalid", "169.254.1.2", "192.0.2.3", "127.0.0.1", "8.8.8.8", address]),
            ):
                self.assertEqual(get_host_ip(), address)

    def test_first_valid_address_remains_the_fallback(self):
        for addresses, expected in (
            (["invalid", "8.8.8.8", "169.254.1.2"], "8.8.8.8"),
            (["169.254.1.2"], "169.254.1.2"),
            (["127.0.0.1"], "127.0.0.1"),
            (["172.15.255.255", "172.32.0.0"], "172.15.255.255"),
            (["invalid"], None),
            ([], None),
        ):
            with self.subTest(addresses=addresses), patch(
                "joydance.config_handler.socket.gethostbyname_ex", return_value=("host", [], addresses),
            ):
                self.assertEqual(get_host_ip(), expected)

    def test_address_lookup_failure_returns_no_address(self):
        with patch("joydance.config_handler.socket.gethostbyname_ex", side_effect=OSError("Offline")):
            self.assertIsNone(get_host_ip())
