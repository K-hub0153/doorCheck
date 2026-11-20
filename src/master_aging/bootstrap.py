"""可独立启动，也可由统一工具选择页延迟导入。"""
import sys


def main():
    from PyQt5.QtWidgets import QApplication
    from .gui.main_window import MainWindow
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
