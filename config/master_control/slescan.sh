#!/bin/bash

# ================= 配置区域 =================
# 获取脚本第一个参数作为扫描时间（单位：秒）
SCAN_DURATION=${1:-5}

TOOL_CMD="sparklinkctrl"   # 交互式控制工具路径
DAEMON_CMD="sparklinkd"    # 星闪后台服务路径
# ===========================================

echo "=== 检查并终止已存在的 $DAEMON_CMD 进程 ==="
if pgrep -x "$DAEMON_CMD" > /dev/null; then
    echo ">> 发现运行中的 $DAEMON_CMD，正在终止..."
    pkill -x "$DAEMON_CMD"
    sleep 1  # 等待进程退出
    # 强制 kill（如果未退出）
    if pgrep -x "$DAEMON_CMD" > /dev/null; then
        echo ">> 未正常退出，发送 SIGKILL..."
        pkill -9 -x "$DAEMON_CMD"
        sleep 1
    fi
    echo ">> 已清理旧进程。"
else
    echo ">> 未发现运行中的 $DAEMON_CMD。"
fi

echo "=== 星闪（SparkLink）自动化扫描任务开始 ==="
echo ">> 扫描持续时间设置为: ${SCAN_DURATION} 秒"

echo "--- [1/4] 启动星闪后台服务 ($DAEMON_CMD) ---"
# 1. 启动 sparklinkd
$DAEMON_CMD &

# 2. 获取进程ID (PID)，用于最后清理
DAEMON_PID=$!
echo ">> 服务已启动，PID: $DAEMON_PID"

# 等待服务初始化完成（可根据实际情况调整）
sleep 2

echo "--- [2/4] 执行 SparkLink AT 指令序列 ---"

# 3. 通过管道发送 AT 指令序列
(
    echo "AT+SLEENABLE"
    sleep 2

    echo "AT+SLEATCOMMONREGCBK"
    sleep 1
    
    echo "AT+SSAPCREGCBK"
    sleep 1

    echo "AT+SLESETSCANPAR=1,0x48,0x48"
    sleep 1

    echo "AT+SLESTARTSCAN"
    
    # 动态等待指定扫描时间
    sleep $SCAN_DURATION

    echo "AT+SLESTOPSCAN"
    sleep 1

    echo "AT+SLEDISABLE"
    sleep 1

    # 退出交互工具
    echo "quit"
    echo "exit"
) | $TOOL_CMD

echo "--- [3/4] 扫描结束，清理工具进程 ---"
# 强制确保 sparklinkctrl 已退出
pkill -x "$TOOL_CMD" > /dev/null 2>&1

echo "--- [4/4] 停止星闪后台服务 ---"
# 杀掉最初启动的服务进程
if ps -p $DAEMON_PID > /dev/null; then
    kill $DAEMON_PID
    wait $DAEMON_PID 2>/dev/null  # 等待进程真正退出
    echo ">> 服务 (PID $DAEMON_PID) 已成功停止。"
else
    echo ">> 服务似乎已经提前退出了。"
fi

echo "=== 星闪扫描任务全部完成 ==="