#!/bin/bash
#===============================================================================
# Xray 客户端启动脚本 (带路由管理)
# 功能：启动 Xray TUN 模式，设置路由，退出时恢复
# 依赖：xray 二进制位于 ./bin/xray，配置位于 ./etc/client-config.json
# 运行：sudo ./start_client.sh
#===============================================================================

set -e

# --- 路径定义 ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
XRAY_BIN="$SCRIPT_DIR/bin/xray"
CONFIG_FILE="$SCRIPT_DIR/etc/client-config.json"
TUN_DEV="xray0"                      # 默认 tun 设备名，可根据配置调整
TUN_IP="10.0.0.1"

#server ip
SERVER_IP=$(cat etc/client-config.json| jq -r ".outbounds[0].settings.address")
#本地GATEWAY以及网卡
read GATEWAY INTERFACE <<< $(ip -4 route show default | awk '{print $3, $5}')

# --- 颜色输出 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# --- 检查必要文件 ---
check_prerequisites() {
    if [[ ! -x "$XRAY_BIN" ]]; then
        echo -e "${RED}错误: Xray 可执行文件不存在或无执行权限: $XRAY_BIN${NC}"
        exit 1
    fi
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo -e "${RED}错误: 配置文件不存在: $CONFIG_FILE${NC}"
        exit 1
    fi
    # 检查 root 权限（tun 和路由需要）
    if [[ $EUID -ne 0 ]]; then
        echo -e "${YELLOW}警告: 此脚本需要 root 权限才能操作 TUN 设备和路由表。${NC}"
        echo -e "请使用 sudo 运行。"
        exit 1
    fi
}

# --- 启动 Xray（后台运行） ---
start_xray() {
    echo -e "${GREEN}>>> 启动 Xray...${NC}"
    # 使用 nohup 避免挂断，输出日志到当前目录
    "$XRAY_BIN" run -c "$CONFIG_FILE" &
    XRAY_PID=$!
    echo -e "Xray PID: $XRAY_PID"
    # 等待进程稳定
    sleep 2
    if ! kill -0 "$XRAY_PID" 2>/dev/null; then
        echo -e "${RED}错误: Xray 启动失败，请查看 xray.log${NC}"
        exit 1
    fi
}

# --- 等待 TUN 设备出现并设置其 IP ---
wait_for_tun() {
    local timeout=15
    echo -n "等待 TUN 设备 $TUN_DEV 就绪..."
    while [[ $timeout -gt 0 ]]; do
        if ip link show "$TUN_DEV" >/dev/null 2>&1; then
            echo -e " ${GREEN}已就绪${NC}"
            # 设置设备 IP
            ip addr add $TUN_IP dev xray0
            echo -e "TUN 设备 IP: $TUN_IP"
            return 0
        fi
        sleep 1
        ((timeout--))
    done
    echo -e " ${RED}超时${NC}"
    echo -e "${RED}错误: TUN 设备 $TUN_DEV 未在指定时间内出现。${NC}"
    return 1
}

# --- 设置路由（备份原默认路由，添加新默认路由到 TUN） ---
setup_routing() {
    echo -e "${GREEN}>>> 设置路由...${NC}"
    # 删除当前默认路由（如果存在）
    route del default
    # 添加新默认路由指向 TUN 设备
    route add $SERVER_IP gw $GATEWAY dev $INTERFACE metric 5
    route add $TUN_IP dev $TUN_DEV metric 0
    route add default gw $TUN_IP dev $TUN_DEV metric 10

    echo -e "✅ 默认路由已切换到 $TUN_DEV ($TUN_IP)"
}

# --- 清理函数（退出时调用） ---
cleanup() {
    echo -e "\n${YELLOW}>>> 正在清理...${NC}"
    # 恢复路由
    route del $SERVER_IP gw $GATEWAY dev $INTERFACE
    route add default gw $GATEWAY dev $INTERFACE
    echo -e "✅ 路由已恢复"
    echo -e "${GREEN}清理完成${NC}"
}

# --- 信号捕获 ---
trap cleanup EXIT

# --- 主流程 ---
main() {
    check_prerequisites
    start_xray
    if ! wait_for_tun; then
        # TUN 设备未就绪，终止 Xray
        kill "$XRAY_PID" 2>/dev/null || true
        exit 1
    fi
    setup_routing
    echo -e "\n${GREEN}🎉 Xray 已运行，路由已设置。按 Ctrl+C 退出并清理。${NC}"
    # 等待 Xray 进程结束（如果 Xray 自行退出则脚本退出）
    wait "$XRAY_PID"
}

main