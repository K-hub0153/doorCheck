#!/usr/bin/env python3
"""
智能设备产测工具打包脚本
支持 Windows、macOS、Linux 跨平台打包
"""

import sys
import os
import platform
import subprocess
import shutil
import yaml
from pathlib import Path
from typing import Optional, List, Tuple


class BuildScript:
    def __init__(self):
        self.system = platform.system()
        self.project_root = Path(__file__).parent.absolute()
        self.dist_dir = self.project_root / "dist"
        self.build_dir = self.project_root / "build"
        self.app_name = "智能设备产测工具"
        self.version = self._load_version()

        # 定义需要检查的依赖
        self.required_modules = [
            "PyQt5",
            "paho.mqtt.client",
            "zeroconf",
            "requests",
            "yaml",
            "amqtt",
            "flask",
            "PIL",
            "qrcode",
            "paramiko",
            "psutil",
            "openpyxl",
        ]

        # 需要复制到 dist 目录的外部资源（不再打包进 exe）
        self.external_dirs = [
            ("config", "config"),
            ("certs", "certs"),
        ]

        # 定义需要显式导入的隐藏模块
        self.hidden_imports = [
            "zeroconf",
            "paho.mqtt.client",
            "PyQt5",
            "PyQt5.QtCore",
            "PyQt5.QtGui",
            "PyQt5.QtWidgets",
            "requests",
            "yaml",
            "amqtt",
            "amqtt.broker",
            "flask",
            "PIL",
            "qrcode",
            "paramiko",
            "psutil",
            "openpyxl",
        ]

        if self.system == "Windows":
            self.required_modules.append("win32print")
            self.hidden_imports.extend(["win32print", "win32ui", "win32con"])

        # 定义需要收集所有依赖的包
        self.collect_all = [
            "zeroconf",
            "paho.mqtt",
            "amqtt",
        ]

    def _load_version(self) -> str:
        """从 config.yaml 读取版本号"""
        config_file = self.project_root / "config" / "config.yaml"
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                version = config.get('app', {}).get('version', '1.0')
                return str(version)
        except Exception as e:
            print(f"警告: 无法读取版本号，使用默认版本 1.0: {e}")
            return "1.0"

    def print_header(self, message: str):
        """打印标题"""
        print("\n" + "=" * 50)
        print(message)
        print("=" * 50 + "\n")

    def print_step(self, step: int, total: int, message: str):
        """打印步骤"""
        print(f"[{step}/{total}] {message}")

    def print_success(self, message: str):
        """打印成功消息"""
        print(f"OK {message}")

    def print_error(self, message: str):
        """打印错误消息"""
        print(f"FAIL {message}", file=sys.stderr)

    def print_warning(self, message: str):
        """打印警告消息"""
        print(f"WARN {message}")

    def check_python_version(self) -> bool:
        """检查Python版本"""
        version = sys.version_info
        if version.major < 3 or (version.major == 3 and version.minor < 8):
            self.print_error(f"Python版本过低: {version.major}.{version.minor}")
            self.print_error("需要 Python 3.8 或更高版本")
            return False

        self.print_success(f"Python版本: {version.major}.{version.minor}.{version.micro}")
        return True

    def check_module(self, module_name: str) -> bool:
        """检查模块是否安装"""
        try:
            __import__(module_name)
            return True
        except ImportError:
            return False

    def check_dependencies(self) -> bool:
        """检查所有依赖"""
        self.print_step(1, 4, "检查依赖...")

        missing_modules = []
        for module in self.required_modules:
            if not self.check_module(module):
                missing_modules.append(module)
                self.print_error(f"缺少模块: {module}")
            else:
                self.print_success(f"模块已安装: {module}")

        if missing_modules:
            print("\n" + "=" * 50)
            self.print_error("缺少必要的Python依赖包")
            print("\n请运行以下命令安装:")
            if self.system == "Windows":
                print("  pip install -r requirements.txt")
            else:
                print("  pip3 install -r requirements.txt")
            print("=" * 50)
            return False

        # 检查 pyinstaller
        if not self.check_module("PyInstaller"):
            self.print_error("缺少 pyinstaller")
            print("\n请运行: pip install pyinstaller")
            return False

        self.print_success("所有依赖检查完成")
        return True

    def check_required_files(self) -> bool:
        """检查必要文件是否存在"""
        print("\n检查必要文件...")

        all_exist = True

        # 检查主程序
        main_py = self.project_root / "main.py"
        if not main_py.exists():
            self.print_error(f"主程序不存在: {main_py}")
            all_exist = False
        else:
            self.print_success("主程序: main.py")

        # 检查配置文件
        config_file = self.project_root / "config" / "config.yaml"
        if not config_file.exists():
            self.print_warning(f"配置文件不存在: {config_file}")
            self.print_warning("打包后程序可能无法正常运行")
        else:
            self.print_success("配置文件: config/config.yaml")

        qrcode_config = self.project_root / "config" / "printer" / "config.yaml"
        if not qrcode_config.is_file():
            self.print_error(f"扫码打印配置文件不存在: {qrcode_config}")
            all_exist = False
        qrcode_background = self.project_root / "config" / "printer" / "10.1POS sn码.png"
        if not qrcode_background.is_file():
            self.print_error(f"扫码打印底图不存在: {qrcode_background}")
            all_exist = False

        # 主控配置和远端检测脚本随统一 config 目录发布
        for name in ("config.yaml", "blescan.sh", "slescan.sh"):
            resource = self.project_root / "config" / "master_control" / name
            if not resource.is_file():
                self.print_error(f"主控资源不存在: {resource}")
                all_exist = False
        master_font = self.project_root / "assets" / "fonts" / "label.ttf"
        if not master_font.is_file():
            self.print_error(f"共享标签字体不存在: {master_font}")
            all_exist = False

        # 检查证书文件
        certs_dir = self.project_root / "certs"
        if not certs_dir.exists():
            self.print_warning(f"证书目录不存在: {certs_dir}")
            self.print_warning("如需SSL功能，请先生成证书")
        else:
            cert_files = ["ca.crt", "mqtt_server.crt", "mqtt_server.key"]
            for cert_file in cert_files:
                cert_path = certs_dir / cert_file
                if cert_path.exists():
                    self.print_success(f"证书: certs/{cert_file}")
                else:
                    self.print_warning(f"证书缺失: certs/{cert_file}")

        return all_exist

    def build_pyinstaller_command(self) -> List[str]:
        """构建 pyinstaller 命令"""
        # 带版本号的文件名
        app_name_with_version = f"{self.app_name}_{self.version}"

        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--windowed",
            "--name", app_name_with_version,
        ]

        # 添加图标
        icon_path = self.project_root / "src" / "ui" / "icon" / "vdian.ico"
        if icon_path.exists():
            cmd.extend(["--icon", str(icon_path)])
            # 将图标文件打包到程序内部根目录
            if self.system == "Windows":
                cmd.extend(["--add-data", f"{icon_path};."])
            else:
                cmd.extend(["--add-data", f"{icon_path}:."])

        # 打包 assets 目录到程序内部
        assets_path = self.project_root / "assets"
        if assets_path.exists():
            if self.system == "Windows":
                cmd.extend(["--add-data", f"{assets_path};assets"])
            else:
                cmd.extend(["--add-data", f"{assets_path}:assets"])

        # 不再通过 --add-data 打包 certs/config，改为复制到 dist 目录

        # 添加隐藏导入
        for module in self.hidden_imports:
            cmd.extend(["--hidden-import", module])

        # 添加收集所有依赖
        for package in self.collect_all:
            cmd.extend(["--collect-all", package])

        # 添加主程序
        cmd.append("main.py")

        return cmd

    def run_pyinstaller(self) -> bool:
        """运行 pyinstaller"""
        self.print_step(2, 4, "开始打包...")

        cmd = self.build_pyinstaller_command()

        print("\n执行命令:")
        print(" ".join(cmd))
        print()

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                check=True,
                text=True
            )
            return result.returncode == 0
        except subprocess.CalledProcessError as e:
            self.print_error(f"打包失败: {e}")
            return False
        except FileNotFoundError:
            self.print_error("找不到 pyinstaller 命令")
            self.print_error("请运行: pip install pyinstaller")
            return False

    def copy_external_resources(self) -> bool:
        """将 certs、config 等外部资源复制到 dist 目录"""
        self.print_step(3, 4, "复制外部资源到 dist 目录...")

        for src, dst in self.external_dirs:
            src_path = self.project_root / src
            dst_path = self.dist_dir / dst

            if not src_path.exists():
                self.print_warning(f"源目录不存在，跳过: {src}")
                continue

            # 删除目标目录（如果存在）再复制
            if dst_path.exists():
                shutil.rmtree(dst_path)

            shutil.copytree(src_path, dst_path)

            # 使用字节读写，避免 Windows 文本模式重新引入 CRLF。
            for script_path in dst_path.rglob("*.sh"):
                if not script_path.is_file():
                    continue
                original = script_path.read_bytes()
                normalized = original.replace(b"\r\n", b"\n")
                if normalized != original:
                    script_path.write_bytes(normalized)
                    self.print_success(f"已转换脚本换行格式为 LF: {script_path.relative_to(self.dist_dir)}")
            self.print_success(f"已复制: {src}/ -> dist/{dst}/")

        return True

    def get_output_file(self) -> Optional[Path]:
        """获取输出文件路径"""
        app_name_with_version = f"{self.app_name}_{self.version}"

        if self.system == "Windows":
            return self.dist_dir / f"{app_name_with_version}.exe"
        elif self.system == "Darwin":  # macOS
            app_file = self.dist_dir / f"{app_name_with_version}.app"
            if app_file.exists():
                return app_file
            return self.dist_dir / app_name_with_version
        else:  # Linux
            return self.dist_dir / app_name_with_version

    def show_results(self) -> bool:
        """显示打包结果"""
        self.print_step(4, 4, "打包完成")

        output_file = self.get_output_file()

        if not output_file or not output_file.exists():
            self.print_error("打包失败：找不到输出文件")
            return False

        file_size = output_file.stat().st_size
        size_mb = file_size / (1024 * 1024)

        self.print_header("打包成功！")

        print(f"可执行文件: {output_file}")
        print(f"文件大小: {size_mb:.2f} MB")
        print(f"版本号: {self.version}")
        print()

        print("dist 目录内容:")
        app_name_with_version = f"{self.app_name}_{self.version}"
        print(f"  OK {app_name_with_version}{'.exe' if self.system == 'Windows' else ''}")
        for src, dst in self.external_dirs:
            dst_path = self.dist_dir / dst
            if dst_path.exists():
                print(f"  OK {dst}/ - {self._get_dir_info(dst_path)}")
        print(f"  OK 日志将输出到 dist/logs/")
        print()

        if self.system in ["Darwin", "Linux"]:
            print("注意事项:")
            print("  - 需要 sudo 权限运行（用于TFTP服务器）")
            print(f"  - 运行命令: sudo {output_file}")
        elif self.system == "Windows":
            print("注意事项:")
            print("  - 需要管理员权限运行")
            print("  - 右键 -> 以管理员身份运行")

        return True

    def _get_dir_info(self, dir_path: Path) -> str:
        """获取目录信息"""
        if not dir_path.is_dir():
            return "1 个文件"

        file_count = sum(1 for _ in dir_path.rglob("*") if _.is_file())
        return f"{file_count} 个文件"

    def clean_build_files(self):
        """清理构建文件"""
        print("\n清理构建文件...")

        dirs_to_clean = [
            self.build_dir,
            self.project_root / "__pycache__",
        ]

        app_name_with_version = f"{self.app_name}_{self.version}"
        files_to_clean = [
            self.project_root / f"{app_name_with_version}.spec",
        ]

        for dir_path in dirs_to_clean:
            if dir_path.exists():
                try:
                    shutil.rmtree(dir_path)
                    self.print_success(f"已删除: {dir_path.name}/")
                except Exception as e:
                    self.print_warning(f"无法删除 {dir_path}: {e}")

        for file_path in files_to_clean:
            if file_path.exists():
                try:
                    file_path.unlink()
                    self.print_success(f"已删除: {file_path.name}")
                except Exception as e:
                    self.print_warning(f"无法删除 {file_path}: {e}")

    def build(self) -> bool:
        """执行完整的构建流程"""
        self.print_header(f"门锁产测工具打包脚本 ({self.system})")

        # 检查Python版本
        if not self.check_python_version():
            return False

        # 检查依赖
        if not self.check_dependencies():
            return False

        # 检查必要文件
        if not self.check_required_files():
            response = input("\n是否继续打包? (y/N): ")
            if response.lower() != 'y':
                print("已取消打包")
                return False

        print()

        # 运行打包
        if not self.run_pyinstaller():
            return False

        # 复制外部资源到 dist 目录
        self.copy_external_resources()

        # 显示结果
        success = self.show_results()

        # 清理构建文件
        self.clean_build_files()

        return success


def main():
    """主函数"""
    builder = BuildScript()

    try:
        success = builder.build()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n用户取消打包")
        sys.exit(1)
    except Exception as e:
        print(f"\n打包过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
