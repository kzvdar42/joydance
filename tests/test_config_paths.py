from contextlib import chdir
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from joydance.config_handler import ConfigHandler, get_config_paths, get_datadir


class ConfigLocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.local = self.root / "application" / "config.cfg"
        self.user = self.root / "profile" / "config.cfg"
        self.paths = [str(self.local), str(self.user)]
        host = patch("joydance.config_handler.get_host_ip", return_value="192.168.1.2")
        host.start()
        self.addCleanup(host.stop)
        self.entry = {"name": "New connection", "pairing_method": "old", "host_ip_addr": "192.168.1.2",
                      "console_ip_addr": "192.168.1.3", "pairing_code": ""}

    def seed(self, path, name):
        return ConfigHandler(str(path)).save_connection({**self.entry, "name": name})

    def test_local_file_wins_without_merging_and_changes_survive_restart(self):
        local_entry = self.seed(self.local, "Local")
        self.seed(self.user, "User")
        user_contents = self.user.read_bytes()
        config = ConfigHandler(self.paths)
        self.assertEqual(config.current_cfg_path, str(self.local))
        self.assertEqual(config.connections, [local_entry])
        added = config.save_connection(self.entry)
        self.assertEqual(config.current_cfg_path, str(self.local))
        self.assertEqual(ConfigHandler(self.paths).connections, [local_entry, added])
        config.delete_connection(local_entry["id"])
        self.assertEqual(ConfigHandler(self.paths).connections, [added])
        self.assertEqual(self.user.read_bytes(), user_contents)

    def test_existing_user_file_is_reused_when_local_file_is_missing(self):
        user_entry = self.seed(self.user, "User")
        config = ConfigHandler(self.paths)
        self.assertEqual(config.current_cfg_path, str(self.user))
        added = config.save_connection(self.entry)
        self.assertFalse(self.local.parent.exists())
        self.assertEqual(ConfigHandler(self.paths).connections, [user_entry, added])

    def test_new_configuration_uses_the_application_folder(self):
        config = ConfigHandler(self.paths)
        self.assertIsNone(config.current_cfg_path)
        self.assertTrue(config.save_data())
        self.assertEqual(config.current_cfg_path, str(self.local))
        self.assertTrue(self.local.is_file())
        self.assertFalse(self.user.parent.exists())

    def test_unwritable_application_folder_uses_user_fallback(self):
        temporary_file = tempfile.TemporaryFile

        def probe(*args, **kwargs):
            if Path(kwargs["dir"]) == self.local.parent:
                raise PermissionError("Application folder is read only")
            return temporary_file(*args, **kwargs)

        with patch("joydance.config_handler.tempfile.TemporaryFile", side_effect=probe):
            config = ConfigHandler(self.paths)
            saved = config.save_connection(self.entry)
            self.assertEqual(config.current_cfg_path, str(self.user))
            self.assertEqual(ConfigHandler(self.paths).connections, [saved])
        self.assertFalse(self.local.exists())

    def test_read_only_local_file_does_not_override_existing_user_settings(self):
        self.seed(self.local, "Local")
        user_entry = self.seed(self.user, "User")
        local_contents = self.local.read_bytes()
        with patch("joydance.config_handler.os.access", side_effect=lambda path, mode: path != str(self.local)):
            config = ConfigHandler(self.paths)
            self.assertEqual(config.current_cfg_path, str(self.user))
            self.assertEqual(config.connections, [user_entry])
            added = config.save_connection(self.entry)
            self.assertEqual(ConfigHandler(self.paths).connections, [user_entry, added])
        self.assertEqual(self.local.read_bytes(), local_contents)

    def test_read_only_local_settings_seed_a_new_fallback_only_once(self):
        local_entry = self.seed(self.local, "Local")
        local_contents = self.local.read_bytes()
        with patch("joydance.config_handler.os.access", side_effect=lambda path, mode: path != str(self.local)):
            config = ConfigHandler(self.paths)
            self.assertEqual(config.connections, [local_entry])
            self.assertEqual(config.current_cfg_path, str(self.local))
            added = config.save_connection(self.entry)
            self.assertEqual(config.current_cfg_path, str(self.user))
            reloaded = ConfigHandler(self.paths)
            self.assertEqual(reloaded.current_cfg_path, str(self.user))
            self.assertEqual(reloaded.connections, [local_entry, added])
            reloaded.delete_connection(local_entry["id"])
            self.assertEqual(ConfigHandler(self.paths).connections, [added])
        self.assertEqual(self.local.read_bytes(), local_contents)

    def test_save_failure_does_not_silently_switch_to_another_existing_file(self):
        local_entry = self.seed(self.local, "Local")
        self.seed(self.user, "User")
        original = {path: Path(path).read_bytes() for path in self.paths}
        config = ConfigHandler(self.paths)
        with patch("joydance.config_handler.os.replace", side_effect=PermissionError("File became locked")):
            with self.assertRaises(OSError):
                config.save_connection(self.entry)
        self.assertEqual(config.connections, [local_entry])
        for path in self.paths:
            self.assertEqual(Path(path).read_bytes(), original[path])
        added = config.save_connection(self.entry)
        self.assertEqual(config.current_cfg_path, str(self.local))
        self.assertEqual(ConfigHandler(self.paths).connections, [local_entry, added])
        self.assertEqual(self.user.read_bytes(), original[str(self.user)])

    def test_no_writable_location_reports_failure_without_overwriting_settings(self):
        local_entry = self.seed(self.local, "Local")
        original = self.local.read_bytes()
        with patch.object(ConfigHandler, "_can_write_config", return_value=False):
            config = ConfigHandler(self.paths)
            self.assertEqual(config.connections, [local_entry])
            with self.assertRaises(OSError):
                config.save_connection(self.entry)
        self.assertEqual(self.local.read_bytes(), original)
        self.assertFalse(self.user.exists())

    def test_relative_input_path_stays_fixed_after_working_directory_changes(self):
        local_entry = self.seed(self.local, "Local")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with chdir(self.local.parent):
            config = ConfigHandler("config.cfg")
        with chdir(elsewhere):
            added = config.save_connection(self.entry)
        self.assertEqual(config.current_cfg_path, str(self.local))
        self.assertEqual(ConfigHandler(str(self.local)).connections, [local_entry, added])
        self.assertFalse((elsewhere / "config.cfg").exists())


class ApplicationConfigPathTests(unittest.TestCase):
    def test_source_run_uses_script_folder_instead_of_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "dance.py"
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            with (chdir(elsewhere), patch.object(sys, "frozen", False, create=True),
                  patch("joydance.config_handler.get_datadir", return_value=str(root / "profile"))):
                self.assertEqual(get_config_paths(str(source)), [str(source.parent / "config.cfg"), str(root / "profile" / "config.cfg")])

    def test_frozen_run_uses_executable_folder_instead_of_bundle_or_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "installed" / "JoyDance.exe"
            bundle = root / "_MEI123"
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            with (chdir(elsewhere), patch.object(sys, "frozen", True, create=True),
                  patch.object(sys, "executable", str(executable)), patch.object(sys, "_MEIPASS", str(bundle), create=True),
                  patch("joydance.config_handler.get_datadir", return_value=str(root / "profile"))):
                self.assertEqual(get_config_paths(str(bundle / "dance.py")), [str(executable.parent / "config.cfg"), str(root / "profile" / "config.cfg")])

    def test_windows_user_directory_respects_appdata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "platform", "win32"), patch.dict(os.environ, {"APPDATA": directory}):
            self.assertEqual(get_datadir(), os.path.join(directory, "JoyDance"))

    def test_windows_user_directory_has_a_default_when_appdata_is_unset(self):
        with (tempfile.TemporaryDirectory() as directory, patch.object(sys, "platform", "win32"),
              patch.dict(os.environ, {"APPDATA": ""}), patch("joydance.config_handler.os.path.expanduser", return_value=directory)):
            self.assertEqual(get_datadir(), os.path.join(directory, "AppData", "Roaming", "JoyDance"))
