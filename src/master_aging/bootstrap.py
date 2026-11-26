"""可独立启动，也可由统一工具选择页延迟导入。"""
import sys


def main():
    from PyQt5.QtWidgets import QApplication, QMessageBox
    from .gui.main_window import MainWindow
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(None, '主控老化工具启动失败', str(exc))
        return 1
    window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
