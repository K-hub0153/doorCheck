#!/bin/bash

# ================= 配置区域 =================
# 获取脚本第一个参数作为扫描时间
# ${1:-10} 的意思是：如果参数1存在则使用参数1，否则默认为 10
SCAN_DURATION=${1:-10}

TOOL_CMD="bluetoothctrl"  # 你的交互工具路径
DAEMON_CMD="bluetoothd"   # 你的后台服务路径
# ===========================================

echo "=== 自动化扫描任务开始 ==="
echo ">> 扫描持续时间设置为: ${SCAN_DURATION} 秒"

echo "--- [1/5] 检查并清理已存在的服务进程 ---"
# 1. 检查服务是否已经运行
if pgrep -x "$DAEMON_CMD" > /dev/null; then
    echo ">> 检测到服务已在运行，正在停止旧进程..."
    pkill -x "$DAEMON_CMD"
    sleep 1
    # 强制 kill（如果未正常退出）
    if pgrep -x "$DAEMON_CMD" > /dev/null; then
        echo ">> 未正常退出，发送 SIGKILL..."
        pkill -9 -x "$DAEMON_CMD"
        sleep 1
    fi
    echo ">> 旧进程已停止"
else
    echo ">> 未发现运行中的 $DAEMON_CMD"
fi

echo "--- [2/5] 启动后台服务 ($DAEMON_CMD) ---"
# 2. 启动 bluetoothd
$DAEMON_CMD &

# 3. 获取进程ID (PID)，用于最后清理
DAEMON_PID=$!
echo ">> 服务已启动，PID: $DAEMON_PID"

# 等待服务初始化
sleep 2

echo "--- [3/5] 执行 AT 指令序列 ---"

# 3. 通过管道发送指令序列
(
    # 初始化序列
    echo "AT+BLEENABLE"
    sleep 2
    echo "AT+BLEGAPREGCBK"
    sleep 1
    echo "AT+BLESETSCANPAR=0x48,0x48,1,1,0"
    sleep 1
    
    # 开始扫描
    echo "AT+BLESTARTSCAN"
    
    # === 动态等待时间 ===
    # 这里使用变量来控制 sleep 的时长
    # sleep $SCAN_DURATION
    sleep 2

    # 停止扫描
    echo "AT+BLESTOPSCAN"
    sleep 1
    
    # 退出交互工具
    echo "quit"
    echo "exit"
    
) | $TOOL_CMD

echo "--- [4/5] 扫描结束，清理工具进程 ---"
# 强制确保 bluetoothctrl 退出
pkill -x "bluetoothctrl" > /dev/null 2>&1

echo "--- [5/5] 停止后台服务 ---"
# 4. 杀掉最初启动的服务进程 (BusyBox ps不支持-p参数,用grep替代)
if ps | grep -q "^\s*${DAEMON_PID}\s"; then
    kill $DAEMON_PID
    echo ">> 服务 (PID $DAEMON_PID) 已成功停止。"
else
    echo ">> 服务似乎已经提前退出了。"
fi

echo "=== 全部完成 ==="