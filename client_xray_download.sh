#!/bin/bash
#===============================================================================
# Xray + Geo 文件自动下载/更新脚本
# 功能：将 xray-core、geoip.dat、geosite.dat 维护在 ./bin 目录下
# 用法：chmod +x manage_xray.sh && ./manage_xray.sh
#===============================================================================

set -e

# --- 目录设置 ---
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$BASE_DIR/bin"
mkdir -p "$BIN_DIR"

# --- 检测系统架构 ---
ARCH=$(uname -m)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
case $ARCH in
    x86_64)  ARCH="64" ;;
    aarch64) ARCH="arm64" ;;
    armv7l)  ARCH="armv7" ;;
    *)       echo "不支持的架构: $ARCH"; exit 1 ;;
esac
XRAY_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-${OS}-${ARCH}.zip"

# --- 依赖检查 ---
check_deps() {
    local deps=(curl jq unzip)
    local missing=()
    for cmd in "${deps[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -ne 0 ]; then
        echo "缺少依赖: ${missing[*]}，正在尝试自动安装..."
        if command -v apt &>/dev/null; then
            sudo apt update && sudo apt install -y "${missing[@]}"
        elif command -v yum &>/dev/null; then
            sudo yum install -y "${missing[@]}"
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm "${missing[@]}"
        else
            echo "无法自动安装依赖，请手动安装: ${missing[*]}"
            exit 1
        fi
    fi
}

# --- 获取最新 Xray 版本号 ---
get_latest_xray_version() {
    curl -s https://api.github.com/repos/XTLS/Xray-core/releases/latest | jq -r .tag_name
}

# --- 获取本地 Xray 版本号 ---
get_local_xray_version() {
    if [ -x "$BIN_DIR/xray" ]; then
        "$BIN_DIR/xray" version | head -n1 | awk '{print $2}'
    else
        echo ""
    fi
}

# --- 下载并解压 Xray ---
download_xray() {
    echo "⬇️  下载 Xray: $XRAY_URL"
    curl -# -L -o "$BIN_DIR/xray.zip" "$XRAY_URL"
    unzip -q -o "$BIN_DIR/xray.zip" -d "$BIN_DIR"
    rm "$BIN_DIR/xray.zip"
    chmod +x "$BIN_DIR/xray"
    echo "✅ Xray 已安装至 $BIN_DIR/xray"
}

# --- 更新 Xray（版本比较） ---
update_xray() {
    if [ ! -x "$BIN_DIR/xray" ]; then
        download_xray
        return
    fi
    local latest=$(get_latest_xray_version)
    local current="v$(get_local_xray_version)"
    if [ -z "$latest" ]; then
        echo "⚠️  无法获取最新版本，跳过 Xray 更新"
        return
    fi
    if [ "$latest" != "$current" ]; then
        echo "🔄 Xray 升级: $current → $latest"
        download_xray
    else
        echo "✅ Xray 已是最新版本 ($current)"
    fi
}

# --- 获取 geo 文件的最新发布时间 ---
get_latest_geo_time() {
    curl -s https://api.github.com/repos/Loyalsoldier/v2ray-rules-dat/releases/latest | jq -r .published_at
}

# --- 下载单个 geo 文件 ---
download_geo() {
    local file="$1"
    echo "⬇️  下载 $file ..."
    curl -# -L -o "$BIN_DIR/$file" "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/$file"
    echo "✅ $file 已保存至 $BIN_DIR/$file"
}

# --- 更新单个 geo 文件（通过发布时间比较） ---
update_geo() {
    local file="$1"
    local remote_time
    remote_time=$(get_latest_geo_time)
    if [ -z "$remote_time" ]; then
        echo "⚠️  无法获取 $file 的最新发布时间，跳过更新"
        return
    fi

    if [ ! -f "$BIN_DIR/$file" ]; then
        download_geo "$file"
        return
    fi

    local remote_ts=$(date -d "$remote_time" +%s 2>/dev/null)
    local local_ts=$(stat -c %Y "$BIN_DIR/$file" 2>/dev/null || echo 0)

    if [ -z "$remote_ts" ]; then
        echo "⚠️  时间格式解析失败，跳过 $file 更新"
        return
    fi

    if [ "$remote_ts" -gt "$local_ts" ]; then
        echo "🔄 $file 有更新（远程: $remote_time）"
        download_geo "$file"
    else
        echo "✅ $file 已是最新"
    fi
}

# --- 主流程 ---
main() {
    check_deps
    echo "📁 工作目录: $BIN_DIR"
    update_xray
    update_geo "geoip.dat"
    update_geo "geosite.dat"
    echo "🎉 所有组件已就绪！"
}

main