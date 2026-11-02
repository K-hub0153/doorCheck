import sys
import os
import ctypes
from PyQt5.QtWidgets import QApplication, QDialog
from PyQt5.QtGui import QIcon
from src.ui.startup_dialog import StartupDialog
from src.ui.main_window import MainWindow
from src.ui.speaker_test_window import SpeakerTestWindow
from src.qrcode_print.qrcode_print_window import QRCodePrintWindow
from src.utils.logger import logger
from src.qrcode_print.config import QRCodePrintConfig


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _restart_as_admin(mode):
    params = mode if mode.startswith('--') else f'--mode {mode}'
    if not getattr(sys, 'frozen', False):
        params = f'"{os.path.abspath(__file__)}" {params}'
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(None, 'runas', sys.executable, params,
                           os.path.dirname(os.path.abspath(__file__)), 1)
    return bool(result and int(result) > 32)


def get_icon_path():
    """获取图标路径，兼容开发环境和打包环境"""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'vdian.ico')
    else:
        return os.path.join(os.path.dirname(__file__), 'src', 'ui', 'icon', 'vdian.ico')


def main():
    from src.utils.crash_handler import install_crash_handlers
    install_crash_handlers()
    logger.info("=" * 50)
    logger.info("智能设备产测工具启动")
    logger.info("=" * 50)

    if sys.platform == 'win32':
        from src.utils.paths import get_app_dir
        recovery_path = os.path.join(get_app_dir(), 'logs', 'speaker_sn_network_backup.json')
        if os.path.exists(recovery_path):
            if not _is_admin():
                if _restart_as_admin('--recover-network'):
                    sys.exit(0)
                logger.error(f'需要管理员权限恢复 RJ45 网络，恢复记录保留在 {recovery_path}')
                sys.exit(1)
            from src.network.speaker_sn_dhcp import WindowsEthernetProvisioner

            WindowsEthernetProvisioner.recover(get_app_dir())

    # Windows下设置AppUserModelID，必须在QApplication创建之前设置
    if sys.platform == 'win32':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('vdian.devicecheck.tool')

    app = QApplication(sys.argv)

    # 在QApplication级别设置图标
    icon_path = get_icon_path()
    icon = None
    if os.path.exists(icon_path):
        icon = QIcon(icon_path)
        app.setWindowIcon(icon)
        logger.info(f"应用图标已设置: {icon_path}")
    else:
        logger.warning(f"图标文件不存在: {icon_path}")

    mode_argument = None
    if '--mode' in sys.argv:
        mode_index = sys.argv.index('--mode')
        if mode_index + 1 < len(sys.argv):
            mode_argument = sys.argv[mode_index + 1]

    if mode_argument:
        mode = mode_argument
    else:
        startup_dialog = StartupDialog()
        if icon and not icon.isNull():
            startup_dialog.setWindowIcon(icon)
        result = startup_dialog.exec_()
        if result != QDialog.Accepted or not startup_dialog.selected_mode:
            logger.info("用户取消选择，程序退出")
            sys.exit(0)
        mode = startup_dialog.selected_mode

    if mode == 'speaker_sn_compare' and sys.platform == 'win32' and not _is_admin():
        if _restart_as_admin(mode):
            sys.exit(0)
        logger.warning('音箱序列号比对工具需要管理员权限，用户取消提权。')
        sys.exit(1)

    logger.info(f"用户选择模式: {mode}")

    # 根据选择创建对应的主窗口
    try:
        if mode == 'door':
            logger.info("启动智能门控产测工具")
            window = MainWindow()
        elif mode == 'master_control':
            from src.master_control.bootstrap import _install_crash_handlers
            from src.master_control.gui.main_window import MainWindow as MasterControlWindow

            logger.info("启动主控设备产测工具")
            _install_crash_handlers()
            app.setApplicationName("MC Checker")
            app.setOrganizationName("Weidian")
            window = MasterControlWindow()
        elif mode == 'speaker_indoor':
            logger.info("启动智能室内音箱产测工具")
            window = SpeakerTestWindow(speaker_type='indoor')
        elif mode == 'speaker_outdoor':
            logger.info("启动智能室外音箱产测工具")
            window = SpeakerTestWindow(speaker_type='outdoor')
        elif mode == 'speaker_sn_compare':
            from src.ui.speaker_sn_compare_window import SpeakerSNCompareWindow

            logger.info("启动音箱设备序列号比对工具")
            window = SpeakerSNCompareWindow()
        elif mode == 'qrcode_print':
            logger.info("启动扫码打印工具")
            config = QRCodePrintConfig()
            window = QRCodePrintWindow(config)
        else:
            logger.error(f"未知模式: {mode}")
            sys.exit(1)

        # 在窗口显示前再次确保图标设置正确
        if icon and not icon.isNull():
            window.setWindowIcon(icon)

        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
