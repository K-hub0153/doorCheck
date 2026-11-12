#!/bin/bash

# ================= 配置区域 =================
# 获取脚本第一个参数作为扫描时间
# ${1:-10} 的意思是：如果参数1存在则使用参数1，否则默认为 10
SCAN_DURATION=${1:-3}
BLE_INIT_TIMEOUT=${BLE_INIT_TIMEOUT:-10}

TOOL_CMD="bluetoothctrl"  # 你的交互工具路径
DAEMON_CMD="bluetoothd"   # 你的后台服务路径
EDGE_CORE_CMD="java"
TOOL_LOG="/tmp/blescan_tool_$$.log"
TOOL_INPUT="/tmp/blescan_tool_input_$$"
# ===========================================

trap 'rm -f "$TOOL_LOG" "$TOOL_INPUT"' EXIT

if ! command -v "$TOOL_CMD" > /dev/null 2>&1; then
    echo "[BLE_ERROR] 找不到蓝牙控制工具: $TOOL_CMD" >&2
    exit 127
fi

if ! command -v "$DAEMON_CMD" > /dev/null 2>&1; then
    echo "[BLE_ERROR] 找不到蓝牙后台服务: $DAEMON_CMD" >&2
    exit 127
fi

echo "[$(date '+%H:%M:%S')] === 自动化扫描任务开始 ==="
echo "[$(date '+%H:%M:%S')] >> 扫描持续时间设置为: ${SCAN_DURATION} 秒"
echo "[$(date '+%H:%M:%S')] >> BLE 初始化最长等待时间: ${BLE_INIT_TIMEOUT} 秒"

echo "[$(date '+%H:%M:%S')] --- [1/5] 检查并清理已存在的服务进程 ---"
# 0. 停止Java进程
if pgrep -x "$EDGE_CORE_CMD" > /dev/null; then
    echo "[$(date '+%H:%M:%S')] >> 检测到Java服务已在运行，可能占用蓝牙，正在停止旧进程..."
    pkill  -9 -x "$EDGE_CORE_CMD"
    sleep 1
    echo "[$(date '+%H:%M:%S')] >> Java服务进程已停止"
else
    echo "[$(date '+%H:%M:%S')] >> 未发现运行中的 $EDGE_CORE_CMD"
fi
# 1. 检查服务是否已经运行
if pgrep -x "$DAEMON_CMD" > /dev/null; then
    echo "[$(date '+%H:%M:%S')] >> 检测到服务已在运行，正在停止旧进程..."
    pkill -x "$DAEMON_CMD"
    sleep 1
    # 强制 kill（如果未正常退出）
    if pgrep -x "$DAEMON_CMD" > /dev/null; then
        echo "[$(date '+%H:%M:%S')] >> 未正常退出，发送 SIGKILL..."
        pkill -9 -x "$DAEMON_CMD"
        sleep 1
    fi
    echo "[$(date '+%H:%M:%S')] >> 旧进程已停止"
else
    echo "[$(date '+%H:%M:%S')] >> 未发现运行中的 $DAEMON_CMD"
fi

echo "[$(date '+%H:%M:%S')] --- [2/5] 启动后台服务 ($DAEMON_CMD) ---"
# 2. 启动 bluetoothd
$DAEMON_CMD &

# 3. 获取进程ID (PID)，用于最后清理
DAEMON_PID=$!
echo "[$(date '+%H:%M:%S')] >> 服务已启动，PID: $DAEMON_PID"

# 等待服务初始化
sleep 2

echo "[$(date '+%H:%M:%S')] --- [3/5] 执行 AT 指令序列 ---"

# 统计 bluetoothctrl 提示符出现次数。每条 AT 指令执行完成后会重新打印提示符，
# 因而可以动态判断初始化/命令是否完成，不再依赖固定 sleep 猜测时序。
prompt_count() {
    grep -o 'bluetoothctrl>' "$TOOL_LOG" 2>/dev/null | wc -l | tr -d ' '
}

wait_for_prompt() {
    target_count=$1
    timeout_seconds=$2
    stage_name=$3
    start_time=$(date +%s)

    while [ "$(prompt_count)" -lt "$target_count" ]; do
        if ! kill -0 "$TOOL_PIPE_PID" 2>/dev/null; then
            echo "[BLE_ERROR] $TOOL_CMD 在${stage_name}期间提前退出" >&2
            return 1
        fi
        now=$(date +%s)
        if [ $((now - start_time)) -ge "$timeout_seconds" ]; then
            echo "[BLE_ERROR] ${stage_name}等待超时（${timeout_seconds}秒）" >&2
            return 1
        fi
        sleep 0.2
    done
    return 0
}

send_and_wait() {
    command=$1
    timeout_seconds=$2
    stage_name=$3
    before_count=$(prompt_count)
    echo "$command" >&3
    wait_for_prompt $((before_count + 1)) "$timeout_seconds" "$stage_name"
}

mkfifo "$TOOL_INPUT"
set -o pipefail
"$TOOL_CMD" < "$TOOL_INPUT" 2>&1 | tee "$TOOL_LOG" &
TOOL_PIPE_PID=$!
exec 3> "$TOOL_INPUT"

CONTROL_STATUS=0
if ! wait_for_prompt 1 5 "控制工具启动"; then
    CONTROL_STATUS=21
elif ! send_and_wait "AT+BLEENABLE" "$BLE_INIT_TIMEOUT" "BLE 初始化"; then
    CONTROL_STATUS=20
elif ! send_and_wait "AT+BLEGAPREGCBK" 5 "BLE 回调注册"; then
    CONTROL_STATUS=22
elif ! send_and_wait "AT+BLESETSCANPAR=0x48,0x48,1,1,0" 5 "BLE 扫描参数设置"; then
    CONTROL_STATUS=23
elif ! send_and_wait "AT+BLESTARTSCAN" 5 "BLE 启动扫描"; then
    CONTROL_STATUS=24
else
    sleep "$SCAN_DURATION"
    if ! send_and_wait "AT+BLESTOPSCAN" 5 "BLE 停止扫描"; then
        CONTROL_STATUS=25
    fi
fi

# 无论成功失败都让交互工具正常退出，并关闭 FIFO 写端。
echo "quit" >&3
echo "exit" >&3
exec 3>&-
wait "$TOOL_PIPE_PID"
TOOL_STATUS=$?

if [ "$CONTROL_STATUS" -ne 0 ]; then
    TOOL_STATUS=$CONTROL_STATUS
fi

if [ "$TOOL_STATUS" -ne 0 ]; then
    echo "[BLE_ERROR] $TOOL_CMD 异常退出，退出码: $TOOL_STATUS" >&2
fi

# bluetoothctrl 会为真正执行的命令输出“Starting AT+...”。若只有 BLEENABLE
# 而没有 BLESTARTSCAN，说明初始化仍未完成，后续输入被丢弃，不能误报为零设备。
if [ "$CONTROL_STATUS" -eq 0 ] \
        && ! grep -Eq 'Starting[[:space:]]+AT\+BLESTARTSCAN|ScanResultCallback' "$TOOL_LOG"; then
    echo "[BLE_ERROR] BLE 初始化未完成，AT+BLESTARTSCAN 未执行" >&2
    if [ "$TOOL_STATUS" -eq 0 ]; then
        TOOL_STATUS=20
    fi
fi

echo "[$(date '+%H:%M:%S')] --- [4/5] 扫描结束，清理工具进程 ---"
# 强制确保 bluetoothctrl 退出
pkill -x "bluetoothctrl" > /dev/null 2>&1

echo "[$(date '+%H:%M:%S')] --- [5/5] 停止后台服务 ---"
# 4. 杀掉最初启动的服务进程 (BusyBox ps不支持-p参数,用grep替代)
if ps | grep -q "^\s*${DAEMON_PID}\s"; then
    kill $DAEMON_PID
    echo "[$(date '+%H:%M:%S')] >> 服务 (PID $DAEMON_PID) 已成功停止。"
else
    echo "[$(date '+%H:%M:%S')] >> 服务似乎已经提前退出了。"
fi

echo "[$(date '+%H:%M:%S')] === 全部完成 ==="
exit "$TOOL_STATUS"
