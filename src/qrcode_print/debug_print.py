"""
打印机调试脚本 - 打印测试内容 112233
提供多种测试模式和详细的调试信息
"""
import sys
import os
import io

# 设置 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import win32print
from datetime import datetime
from src.utils.logger import logger


def check_printer_status():
    """检查打印机状态"""
    printer_name = win32print.GetDefaultPrinter()
    print(f"\n{'='*60}")
    print(f"打印机: {printer_name}")

    try:
        hPrinter = win32print.OpenPrinter(printer_name)
        info = win32print.GetPrinter(hPrinter, 2)
        win32print.ClosePrinter(hPrinter)

        print(f"状态码: {info['Status']}")
        print(f"队列任务数: {info['cJobs']}")

        if info['Status'] == 0:
            print("状态: 就绪 ✓")
            return True
        else:
            print(f"状态: 异常 (代码 {info['Status']})")
            return False
    except Exception as e:
        print(f"检查失败: {e}")
        return False


def build_simple_tspl(content="112233"):
    """
    构建简化的 TSPL 指令
    只包含必要的命令，便于调试
    """
    tspl = ""
    tspl += "SIZE 50 mm, 30 mm\n"
    tspl += "GAP 2 mm, 0 mm\n"
    tspl += "DIRECTION 0\n"  # 0=正常方向，1=旋转180度
    tspl += "CLS\n"

    # 大号文本 - 中央显示
    tspl += f'TEXT 100,100,"4",0,2,2,"{content}"\n'

    # 二维码
    tspl += f'QRCODE 300,50,M,8,A,0,M2,S7,"{content}"\n'

    tspl += "PRINT 1,1\n"

    return tspl


def build_standard_tspl(content="112233"):
    """
    构建标准的 TSPL 指令（与现有代码一致）
    """
    tspl = ""
    tspl += "CLS\n"
    tspl += "SIZE 50 mm, 30 mm\n"
    tspl += "GAP 2 mm, 0 mm\n"
    tspl += "DIRECTION 1\n"
    tspl += "SPEED 4\n"
    tspl += "DENSITY 10\n"
    tspl += "REFERENCE 0,0\n"
    tspl += "SET TEAR ON\n"
    tspl += "CLS\n"

    # 日期
    date_str = datetime.now().strftime("%Y/%m/%d")
    tspl += f'TEXT 120,124,"1",0,1,1,"{date_str}"\n'

    # 序列号
    tspl += f'TEXT 34,178,"1",0,1,1,"SN:{content}"\n'

    # 二维码
    tspl += f'QRCODE 245,69,M,6,A,0,M2,S7,"{content}"\n'

    tspl += "PRINT 1,1\n"
    tspl += "CUT\n"

    return tspl


def build_minimal_tspl(content="112233"):
    """
    构建最简 TSPL 指令（用于测试打印机是否响应）
    """
    tspl = ""
    tspl += "SIZE 50 mm, 30 mm\n"
    tspl += "CLS\n"
    tspl += f'TEXT 50,50,"3",0,1,1,"{content}"\n'
    tspl += "PRINT 1\n"

    return tspl


def send_raw_tspl(tspl_commands, printer_name=None):
    """发送原始 TSPL 指令到打印机"""
    if printer_name is None:
        printer_name = win32print.GetDefaultPrinter()

    try:
        print(f"\n发送打印指令到: {printer_name}")
        print(f"指令长度: {len(tspl_commands)} 字节")

        # 编码为字节
        data = tspl_commands.encode('utf-8')

        # 打开打印机
        hPrinter = win32print.OpenPrinter(printer_name)

        try:
            # 开始文档
            win32print.StartDocPrinter(hPrinter, 1, ("Debug Print", None, "RAW"))

            # 开始页面
            win32print.StartPagePrinter(hPrinter)

            # 写入数据
            win32print.WritePrinter(hPrinter, data)

            # 结束页面
            win32print.EndPagePrinter(hPrinter)

            # 结束文档
            win32print.EndDocPrinter(hPrinter)

            print("✓ 打印任务已发送")
            return True

        finally:
            win32print.ClosePrinter(hPrinter)

    except Exception as e:
        print(f"✗ 打印失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_commands(tspl_commands, filename="debug_print_commands.txt"):
    """保存指令到文件"""
    output_dir = "logs"
    os.makedirs(output_dir, exist_ok=True)

    filepath = os.path.join(output_dir, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(tspl_commands)

    print(f"指令已保存到: {filepath}")
    return filepath


def main():
    print("="*60)
    print("打印机调试脚本 - 测试内容: 112233")
    print("="*60)

    # 解析参数
    mode = "standard"  # minimal, simple, standard
    actually_print = False
    content = "112233"

    for arg in sys.argv[1:]:
        if arg == "--print" or arg == "-p":
            actually_print = True
        elif arg == "--minimal" or arg == "-m":
            mode = "minimal"
        elif arg == "--simple" or arg == "-s":
            mode = "simple"
        elif arg == "--standard" or arg == "-std":
            mode = "standard"
        elif not arg.startswith("-"):
            content = arg

    print(f"\n模式: {mode}")
    print(f"内容: {content}")
    print(f"实际打印: {'是' if actually_print else '否（仅生成指令）'}")

    # 1. 检查打印机状态
    printer_ok = check_printer_status()

    # 2. 生成 TSPL 指令
    print(f"\n{'='*60}")
    print(f"生成 TSPL 指令 (模式: {mode})")
    print("="*60)

    if mode == "minimal":
        tspl = build_minimal_tspl(content)
        print("使用最简指令（仅文本）")
    elif mode == "simple":
        tspl = build_simple_tspl(content)
        print("使用简化指令（文本 + 二维码，正常方向）")
    else:  # standard
        tspl = build_standard_tspl(content)
        print("使用标准指令（与现有代码一致）")

    print(f"\n{'-'*60}")
    print(tspl)
    print(f"{'-'*60}\n")

    # 3. 保存指令
    filename = f"debug_{mode}_{content}.txt"
    save_commands(tspl, filename)

    # 4. 打印
    if actually_print:
        if not printer_ok:
            print("\n⚠ 警告: 打印机状态异常，但仍尝试打印...")

        print(f"\n{'='*60}")
        print("发送打印任务")
        print("="*60)

        success = send_raw_tspl(tspl)

        if success:
            print("\n✓ 打印任务已发送到打印机")
            print("\n请检查:")
            print("  1. 打印机是否开始工作（指示灯、声音）")
            print("  2. 是否有纸张输出")
            print("  3. 打印队列状态")
            print("\n如果没有输出，可能原因:")
            print("  - 打印机缺纸")
            print("  - 需要校准（按住打印机按钮3-5秒）")
            print("  - 纸张传感器故障")
        else:
            print("\n✗ 打印失败")
    else:
        print("\n提示: 使用 --print 参数实际打印")
        print(f"示例: python tools/debug_print.py --print")
        print(f"      python tools/debug_print.py --simple --print")
        print(f"      python tools/debug_print.py --minimal --print")

    print("\n" + "="*60)
    print("调试完成")
    print("="*60)


if __name__ == "__main__":
    main()
