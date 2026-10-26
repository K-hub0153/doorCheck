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

    # 显示启动选择对话框
    startup_dialog = StartupDialog()
    # 确保启动对话框也有图标
    if icon and not icon.isNull():
        startup_dialog.setWindowIcon(icon)

    result = startup_dialog.exec_()

    if result != QDialog.Accepted or not startup_dialog.selected_mode:
        logger.info("用户取消选择，程序退出")
        sys.exit(0)

    mode = startup_dialog.selected_mode
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
