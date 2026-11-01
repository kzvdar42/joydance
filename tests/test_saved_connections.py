import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from joydance.config_handler import ConfigHandler


class SavedConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.cfg"
        self.host = patch("joydance.config_handler.get_host_ip", return_value="192.168.1.2")
        self.host.start()
        self.addCleanup(self.host.stop)
        self.connection = {
            "name": "Living room", "pairing_method": "old", "host_ip_addr": "192.168.1.2",
            "console_ip_addr": "192.168.1.3", "pairing_code": "",
            "console_url": "http://192.168.1.3:9000/game?token=hello%20world",
        }

    def test_add_edit_delete_and_reload_preserve_connections_and_custom_urls(self):
        config = ConfigHandler(str(self.path))
        first = config.save_connection({**self.connection, "console_urls": ["ws://127.0.0.1:9000/game/smartphone"]})
        self.assertNotIn("console_urls", first)
        second = config.save_connection({**self.connection, "name": "Bedroom", "console_url": None})
        edited = config.save_connection({**first, "name": "Main room"})
        config = ConfigHandler(str(self.path))
        self.assertEqual([item["name"] for item in config.connections], ["Main room", "Bedroom"])
        self.assertEqual(config.connections[0]["id"], first["id"])
        self.assertEqual(config.connections[0]["console_url"], "ws://192.168.1.3:9000/game/smartphone?token=hello%20world")
        config.delete_connection(second["id"])
        self.assertEqual(ConfigHandler(str(self.path)).connections, [edited])

    def test_previous_configuration_is_migrated_only_once(self):
        self.path.write_text("[joydance]\npairing_method = old\nconsole_ip_addr = 192.168.1.3\n", encoding="utf-8")
        config = ConfigHandler(str(self.path))
        self.assertEqual(len(config.connections), 1)
        self.assertEqual(config.connections[0]["name"], "Previous connection")
        config.delete_connection(config.connections[0]["id"])
        reloaded = ConfigHandler(str(self.path))
        self.assertEqual(reloaded.connections, [])
        self.assertEqual(reloaded.data["console_ip_addr"], "192.168.1.3")

    def test_last_used_settings_do_not_replace_the_saved_list(self):
        config = ConfigHandler(str(self.path))
        saved = config.save_connection(self.connection)
        config.data = {"pairing_method": "jdmo", "host_ip_addr": "192.168.1.2", "pairing_code": "012345"}
        self.assertEqual(ConfigHandler(str(self.path)).connections, [saved])

    def test_invalid_entries_and_unknown_updates_do_not_change_the_list(self):
        config = ConfigHandler(str(self.path))
        saved = config.save_connection(self.connection)
        for changes in (
            {"name": " "}, {"name": "x" * 81}, {"pairing_method": "unknown"},
            {"console_ip_addr": ""}, {"pairing_method": []}, {"console_url": "http://8.8.8.8/"},
            {"console_url": "http://192.168.1.4/"}, {"id": "invalid"},
            {"pairing_method": "jdmo", "pairing_code": "abc", "console_url": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config.save_connection({**self.connection, **changes})
        with self.assertRaises(KeyError):
            config.save_connection({**saved, "id": "0" * 32})
        with self.assertRaises(KeyError):
            config.delete_connection("missing")
        self.assertEqual(ConfigHandler(str(self.path)).connections, [saved])

    def test_failed_atomic_write_preserves_file_and_in_memory_list(self):
        config = ConfigHandler(str(self.path))
        saved = config.save_connection(self.connection)
        original = self.path.read_bytes()
        with patch("joydance.config_handler.os.replace", side_effect=PermissionError("Read only")):
            with self.assertRaises(OSError):
                config.save_connection({**saved, "name": "Changed"})
            with self.assertRaises(OSError):
                config.delete_connection(saved["id"])
        self.assertEqual(config.connections, [saved])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_saved_data_is_not_overwritten(self):
        for items in ("invalid json", "{}", json.dumps([{"name": "broken"}])):
            with self.subTest(items=items):
                original = "[joydance]\npairing_method = default\n[connections]\nitems = " + items
                self.path.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    ConfigHandler(str(self.path))
                self.assertEqual(self.path.read_text(encoding="utf-8"), original)
