"""合并回归：窗口、配置读写和打包资源隔离；不连接设备或打印机。"""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt5.QtWidgets import QApplication, QPushButton
from src.ui.startup_dialog import StartupDialog
from src.ui.main_window import MainWindow as DoorWindow
from src.ui.speaker_test_window import SpeakerTestWindow
from src.master_control.gui.main_window import MainWindow as MasterWindow
from src.master_control.utils.config import config as master_config
from src.master_control.utils import paths
from src.utils.config import Config as DeviceConfig
from src.utils.paths import get_app_dir
from build import BuildScript


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_entry_order_and_selection(self):
        modes = ['master_control', 'door', 'speaker_indoor', 'speaker_outdoor']
        for index, mode in enumerate(modes):
            dialog = StartupDialog()
            buttons = dialog.findChildren(QPushButton)
            self.assertEqual(len(buttons), 4)
            self.assertIn('主控设备产测工具', buttons[0].text())
            self.assertIn('智能门控产测工具', buttons[1].text())
            buttons[index].click()
            self.assertEqual(dialog.selected_mode, mode)

    def test_all_windows_share_process_without_module_conflicts(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch('src.master_control.utils.config.runtime_dir', return_value=Path(directory)))
            stack.enter_context(patch('src.ui.main_window.TestRecordStorage'))
            stack.enter_context(patch('src.ui.speaker_test_window.TestRecordDB'))
            for method in ('start_mqtt_broker', 'start_http_server', 'start_tftp_server',
                           'start_device_discovery', 'init_broadcast_mqtt', 'start_heartbeat_monitor'):
                stack.enter_context(patch.object(DoorWindow, method))
            for method in ('start_mqtt_broker', 'start_http_server', 'start_firmware_server',
                           'start_device_discovery', 'start_device_refresh_timer', 'start_device_heartbeat_timer'):
                stack.enter_context(patch.object(SpeakerTestWindow, method))
            windows = [MasterWindow(), DoorWindow(), SpeakerTestWindow('indoor'), SpeakerTestWindow('outdoor')]
            try:
                self.assertEqual(
                    windows[0].windowTitle(),
                    f'主控设备产测工具 - MC Checker v{DeviceConfig().app_version}',
                )
                self.assertEqual(windows[2].current_speaker_type, 'indoor')
                self.assertEqual(windows[3].current_speaker_type, 'outdoor')
                self.assertIs(windows[1].config, windows[2].config)
                self.assertIsNot(windows[1].config, master_config)
            finally:
                for window in reversed(windows):
                    window.close()

    def test_configuration_write_does_not_touch_door_or_speaker(self):
        original = (ROOT / 'config/config.yaml').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'config.yaml'
            target.write_bytes(master_config.config_path.read_bytes())
            old_state = (master_config._config, master_config._config_path, master_config._mtime)
            try:
                with patch('src.master_control.utils.config.config_dir', return_value=Path(directory)):
                    master_config.load_config(force=True)
                    master_config.set('printer.dpi', 300)
                    master_config.save()
                    master_config.load_config(force=True)
                    self.assertEqual(master_config.get('printer.dpi'), 300)
                    self.assertEqual(master_config.config_path, target)
            finally:
                master_config._config, master_config._config_path, master_config._mtime = old_state
        self.assertEqual((ROOT / 'config/config.yaml').read_bytes(), original)
        self.assertIsNot(DeviceConfig(), master_config)

    def test_frozen_resources_and_runtime_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = root / 'bundle'
            bundled_font = bundle / 'assets/fonts/label.ttf'
            bundled_font.parent.mkdir(parents=True)
            bundled_font.write_bytes(b'font')
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, '_MEIPASS', str(bundle), create=True), \
                 patch.object(sys, 'executable', str(root / 'app')):
                self.assertEqual(paths.config_dir(), root / 'config/master_control')
                self.assertEqual(paths.resource_path('assets/fonts/label.ttf'), bundled_font)
                self.assertEqual(paths.runtime_dir(), root / 'logs')
                self.assertEqual(Path(master_config.database_path).parent, root / 'logs')
                external = root / 'assets/fonts/label.ttf'
                external.parent.mkdir(parents=True)
                external.write_bytes(b'override')
                self.assertEqual(paths.resource_path('assets/fonts/label.ttf'), external)

    def test_missing_master_configuration_cannot_load_device_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('src.master_control.utils.config.config_dir', return_value=Path(directory)):
                with self.assertRaises(FileNotFoundError):
                    master_config.load_config(force=True)

    def test_macos_bundle_uses_same_external_configuration_as_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / '工具.app/Contents/MacOS/工具'
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, 'executable', str(executable)):
                self.assertEqual(Path(get_app_dir()), root)
                self.assertEqual(paths.config_dir(), root / 'config/master_control')
                self.assertEqual(paths.runtime_dir(), root / 'logs')

    def test_unified_build_resources_on_all_platforms(self):
        build = BuildScript()
        self.assertEqual(build.version, str(DeviceConfig().app_version))
        self.assertTrue(build.check_required_files())
        self.assertIn(('config', 'config'), build.external_dirs)
        for platform in ('Windows', 'Darwin', 'Linux'):
            build.system = platform
            command = build.build_pyinstaller_command()
            for dependency in ('paramiko', 'psutil', 'openpyxl'):
                self.assertIn(dependency, command)
            sep = ';' if platform == 'Windows' else ':'
            self.assertIn(f'{ROOT / "assets"}{sep}assets', command)


if __name__ == '__main__':
    unittest.main()
