#!/usr/bin/env python3
"""
IPv6 地址自动轮换守护进程（基于流量阈值）
- 自动检测网络接口和 /64 前缀
- 启动时检查系统 IPv6 优先级，异常时自动修正
- 监控当前 IPv6 地址的入+出流量（通过 ip6tables）
- 超阈值时自动切换新地址，旧地址优雅弃用并延迟删除
- 使用连接跟踪（conntrack）判断旧地址是否仍活跃，避免中断长连接
- 非阻塞轮询设计，适合作为 systemd 服务长期运行
- 所有输出通过 logging 记录，便于 journalctl 查看
"""

import subprocess
import time
import random
import re
import sys
import os
import logging
from datetime import datetime, timedelta
import ipaddress
import shutil
import signal

# ================== 可配置参数 ==================
THRESHOLD_BYTES = 4 * 1024 ** 3        # 触发切换的流量阈值（4GB）
CHECK_INTERVAL = 10                    # 主循环检查间隔（秒）
GRACE_PERIOD = 300                     # 旧 IP 宽限期（秒），超时后强制删除
VALID_LFT = 3600                       # 旧 IP 的有效生存时间（秒），需大于 GRACE_PERIOD
# ===============================================

# 配置日志（输出到 stdout，systemd 会捕获到 journal）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 全局状态
current_ip = None
old_ips = []  # 元素为字典：{'ip': str, 'expire_at': datetime}
INTERFACE = None
GATEWAY = None
IPV6_PREFIX = None

def run_cmd(cmd, check=False):
    """执行 shell 命令，返回 stdout 字符串"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if check and result.returncode != 0:
            logger.error(f"命令执行失败: {cmd}\n{result.stderr}")
            sys.exit(1)
        return result.stdout.strip()
    except Exception as e:
        logger.error(f"执行命令异常: {cmd}\n{e}")
        if check:
            sys.exit(1)
        return ""

def ensure_ipv6_preference():
    """
    确保系统优先使用 IPv6。
    由于 glibc 默认已是 IPv6 优先（::/0 优先级 40 > ::ffff:0:0/96 优先级 10），
    本函数仅检查是否存在反向配置（即 IPv4 映射地址优先级高于 IPv6），
    若发现异常则修正；否则保持默认，不做任何改动。
    """
    gai_path = "/etc/gai.conf"
    logger.info("检查系统 IPv6 优先级设置...")

    if not os.path.exists(gai_path):
        logger.info("/etc/gai.conf 不存在，系统将使用 glibc 默认规则（默认 IPv6 优先）")
        return

    try:
        with open(gai_path, 'r') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"读取 {gai_path} 失败: {e}，将保持原有配置")
        return

    lines = content.split('\n')
    ipv6_prec = None
    ipv4map_prec = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('precedence'):
            parts = line.split()
            if len(parts) >= 3:
                prefix = parts[1]
                try:
                    val = int(parts[2])
                except ValueError:
                    continue
                if prefix == '::/0':
                    ipv6_prec = val
                elif prefix == '::ffff:0:0/96':
                    ipv4map_prec = val

    if ipv6_prec is not None and ipv4map_prec is not None:
        if ipv6_prec > ipv4map_prec:
            logger.info(f"系统已配置为 IPv6 优先（::/0 优先级 {ipv6_prec} > ::ffff:0:0/96 优先级 {ipv4map_prec}）")
            return
        else:
            logger.warning(f"检测到异常配置：IPv4 映射地址优先级 ({ipv4map_prec}) 高于或等于 IPv6 ({ipv6_prec})，将进行修正")
    else:
        logger.info("未在 /etc/gai.conf 中找到显式优先级配置，系统将使用 glibc 默认值（默认 IPv6 优先）")
        return

    # 执行修正
    backup_path = gai_path + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S")
    try:
        shutil.copy2(gai_path, backup_path)
        logger.info(f"已备份原文件到 {backup_path}")
    except Exception as e:
        logger.error(f"备份失败: {e}，放弃修改")
        return

    try:
        with open(gai_path, 'a') as f:
            f.write("\n# Corrected by ipv6_rotator: force IPv6 preference\n")
            f.write("precedence ::/0 100\n")
            f.write("precedence ::ffff:0:0/96 10\n")
        logger.info("已修正 /etc/gai.conf，强制 IPv6 优先（::/0 优先级 100 > ::ffff:0:0/96 优先级 10）")
    except Exception as e:
        logger.error(f"写入 {gai_path} 失败: {e}，恢复备份")
        shutil.copy2(backup_path, gai_path)
        return

    logger.info("IPv6 优先配置已生效。当前已运行的进程不受影响，新启动的进程将优先使用 IPv6。")

def auto_detect_interface_gw_and_prefix():
    """
    自动检测默认路由接口，并从该接口获取第一个全局 IPv6 地址，提取 /64 前缀。
    若无默认路由，则遍历所有接口，选择第一个有全局 IPv6 地址的接口。
    返回 (interface, prefix)
    """
    iface = None
    gw = None
    prefix = None
    out = run_cmd("ip -6 route show default")
    if out:
        dev_match = re.search(r'dev\s+(\S+)', out)
        if dev_match:
            iface = dev_match.group(1)
            logger.info(f"通过默认路由检测到接口: {iface}")
            addr_out = run_cmd(f"ip -6 addr show dev {iface} scope global")
            if addr_out:
                match_addr = re.search(r'inet6 ([0-9a-f:]+)/(\d+)', addr_out)
                if match_addr:
                    addr_str = match_addr.group(1)
                    prefix_len = int(match_addr.group(2))
                    if prefix_len == 64:
                        prefix = get_prefix_from_address(addr_str, 64)
                    else:
                        logger.warning(f"接口 {iface} 地址 {addr_str} 的前缀长度不是 /64，而是 /{prefix_len}，将截取为 /64")
                        prefix = get_prefix_from_address(addr_str, 64)
                        if not prefix:
                            logger.error("无法从地址生成 /64 前缀")
                else:
                    logger.warning(f"接口 {iface} 虽然有全局地址，但解析失败")
            else:
                logger.warning(f"默认路由接口 {iface} 没有全局 IPv6 地址，尝试其他接口")

        gw_match = re.search(r'default via\s+(\S+)', out)
        if gw_match :
            gw = dev_match.group(1)
            logger.info(f"通过默认路由检测到网关: {gw}")

    if iface and gw and prefix :
        return iface, gw, prefix

    logger.error("未能成功检测到可用的接口和 /64 前缀")
    sys.exit(1)

def get_prefix_from_address(addr_str, prefix_len=64):
    try:
        if '/' in addr_str:
            addr_obj = ipaddress.IPv6Interface(addr_str)
        else:
            addr_obj = ipaddress.IPv6Interface(f"{addr_str}/{prefix_len}")
        exploded = addr_obj.ip.exploded
        parts = exploded.split(':')
        prefix_parts = parts[:4]
        while len(prefix_parts) < 4:
            prefix_parts.append('0000')
        prefix_str = ':'.join(prefix_parts) + '::'
        return prefix_str
    except Exception as e:
        logger.error(f"解析 IPv6 地址 '{addr_str}' 失败: {e}")
        return None

def get_current_global_ip():
    global INTERFACE
    if not INTERFACE:
        logger.error("INTERFACE 未设置")
        return None
    out = run_cmd(f"ip -6 addr show dev {INTERFACE} | grep inet6 | grep global")
    match = re.search(r'inet6 ([0-9a-f:]+)/\d+', out)
    return match.group(1) if match else None

def generate_new_ip():
    """
    从全局 IPV6_PREFIX（格式如 "2001:db8:1234:5678::"）生成一个随机的 /64 地址。
    返回完整的 8 段 IPv6 地址（无压缩），例如 "2001:db8:1234:5678:abcd:1234:5678:9abc"
    """
    # 去掉末尾可能的 '::' 或 ':'，得到前4段
    base = IPV6_PREFIX
    if base.endswith('::'):
        base = base[:-2]
    elif base.endswith(':'):
        base = base[:-1]
    # 生成64位随机数
    suffix_int = random.getrandbits(64)
    # 拆成4个16位段（注意顺序，低16位是最后一段）
    parts = []
    for _ in range(4):
        parts.append(f"{suffix_int & 0xffff:04x}")
        suffix_int >>= 16
    parts.reverse()  # 反转得到正确顺序
    suffix = ':'.join(parts)
    return f"{base}:{suffix}"

def setup_iptables_rule(ip, action="add"):
    """添加或删除 ip6tables 计数规则（INPUT/OUTPUT）"""
    if action == "add":
        # 先尝试删除可能存在的旧规则，避免重复
        run_cmd(f"ip6tables -D INPUT -d {ip} -j ACCEPT 2>/dev/null")
        run_cmd(f"ip6tables -D OUTPUT -s {ip} -j ACCEPT 2>/dev/null")
        # 添加新规则
        run_cmd(f"ip6tables -I INPUT -d {ip} -j ACCEPT", check=True)
        run_cmd(f"ip6tables -I OUTPUT -s {ip} -j ACCEPT", check=True)
        logger.debug(f"已添加 {ip} 的计数规则")
    else:  # delete
        run_cmd(f"ip6tables -D INPUT -d {ip} 2>/dev/null")
        run_cmd(f"ip6tables -D OUTPUT -s {ip} 2>/dev/null")
        logger.debug(f"已删除 {ip} 的计数规则")

def get_traffic_for_ip(ip):
    """
    读取 ip6tables 中该 IP 的入+出总字节数（修复解析逻辑）
    """
    total = 0
    for chain in ['INPUT', 'OUTPUT']:
        out = run_cmd(f"ip6tables -L {chain} -v -x -n")
        for line in out.split('\n'):
            # 匹配包含 ACCEPT 且包含目标 IP 的行（确保是我们插入的规则）
            if "ACCEPT" in line and ip in line:
                parts = line.split()
                # 格式：pkts bytes target ...，bytes 在第二个字段（索引1）
                if len(parts) > 1 and parts[1].isdigit():
                    total += int(parts[1])
    return total

def is_ip_active_in_conntrack(ip):
    try:
        with open('/proc/net/nf_conntrack', 'r') as f:
            for line in f:
                if f"src={ip}" in line or f"dst={ip}" in line:
                    return True
        return False
    except FileNotFoundError:
        out = run_cmd(f"ss -6 -t -u -e -n | grep -E 'src={ip}|dst={ip}'")
        return bool(out)

def add_new_ip(ip):
    run_cmd(f"ip -6 addr add {ip}/64 dev {INTERFACE}", check=True)
    logger.info(f"已添加新 IP: {ip}")

def deprecate_old_ip(ip):
    run_cmd(f"ip -6 addr change {ip}/64 dev {INTERFACE} preferred_lft 0 valid_lft {VALID_LFT}", check=True)
    logger.info(f"旧 IP {ip} 已弃用（preferred_lft=0），已有连接仍可继续")

def delete_old_ip(ip):
    run_cmd(f"ip -6 addr del {ip}/64 dev {INTERFACE}", check=True)
    logger.info(f"已删除旧 IP: {ip}")


def set_route_src(ip :str) :
    run_cmd(f"ip -6 route replace default via {GATEWAY} dev {INTERFACE} src {ip}")
    logger.info(f"已设置路由src IP: {ip}")


def switch_ip(old_ip :str, need_deprecate_old_ip :bool = True):
    new_ip = generate_new_ip()
    while new_ip == old_ip:
        new_ip = generate_new_ip()

    logger.info(f"开始切换: {old_ip} -> {new_ip}")
    add_new_ip(new_ip)
    setup_iptables_rule(old_ip, action="del")
    setup_iptables_rule(new_ip, action="add")
    set_route_src(new_ip)
    if need_deprecate_old_ip :
        deprecate_old_ip(old_ip)
        expire_at = datetime.now() + timedelta(seconds=GRACE_PERIOD)
        old_ips.append({'ip': old_ip, 'expire_at': expire_at})
        logger.info(f"旧 IP {old_ip} 将在 {GRACE_PERIOD}s 后（{expire_at.strftime('%H:%M:%S')}）被尝试删除")
    return new_ip


def process_old_ips():
    global old_ips
    now = datetime.now()
    still_pending = []
    for entry in old_ips:
        ip = entry['ip']
        expire_at = entry['expire_at']
        if now >= expire_at:
            if is_ip_active_in_conntrack(ip):
                logger.warning(f"旧 IP {ip} 已超时但 conntrack 仍显示活跃，强制删除")
            else:
                logger.info(f"旧 IP {ip} 已超时且 conntrack 无活跃，安全删除")
            setup_iptables_rule(ip, action="del")
            delete_old_ip(ip)
        else:
            still_pending.append(entry)
    old_ips = still_pending

def cleanup_all(ip):
    if ip:
        setup_iptables_rule(ip, action="del")
        logger.info(f"已清理 {ip} 的 ip6tables 规则")
    for entry in old_ips:
        setup_iptables_rule(entry['ip'], action="del")
    logger.info("清理完成")


keeprun = True
def sigterm_handler(signal_num, frame) :
    global keeprun
    keeprun = False


def main():
    global current_ip, keeprun, INTERFACE, GATEWAY, IPV6_PREFIX

    signal.signal(signal.SIGTERM, sigterm_handler)

    if os.geteuid() != 0:
        logger.error("本脚本需要 root 权限运行（请使用 sudo）")
        sys.exit(1)

    logger.info("正在检查并设置系统 IPv6 优先...")
    ensure_ipv6_preference()

    logger.info("开始自动检测网络接口和 IPv6 /64 前缀...")
    INTERFACE, GATEWAY, IPV6_PREFIX = auto_detect_interface_gw_and_prefix()
    logger.info(f"自动检测完成：接口 = {INTERFACE}，/64 前缀 = {IPV6_PREFIX}")

    current_ip = get_current_global_ip()
    if not current_ip:
        logger.error(f"在接口 {INTERFACE} 上未找到全局 IPv6 地址")
        sys.exit(1)

    current_ip = switch_ip(current_ip, False)
    logger.info(f"第一次运行, 保留旧ip, 添加一个新 IP: {current_ip}")

    logger.info(f"启动 IPv6 轮换守护进程，初始 IP: {current_ip}")
    logger.info(f"流量阈值: {THRESHOLD_BYTES/(1024**3):.1f} GB，检查间隔: {CHECK_INTERVAL}s")

    setup_iptables_rule(current_ip, action="add")
    logger.info(f"已为 {current_ip} 添加 ip6tables 计数规则")

    try:
        while keeprun:
            traffic = get_traffic_for_ip(current_ip)
            traffic_gb = traffic / (1024 ** 3)
            logger.info(f"当前 IP {current_ip} 累计流量: {traffic} 字节 ({traffic_gb:.2f} GB)")

            if traffic > THRESHOLD_BYTES :
                current_ip = switch_ip(current_ip)
                logger.info(f"切换完成，新 IP: {current_ip}")
                process_old_ips()
            else:
                process_old_ips()

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logger.info("收到中断信号，正在清理...")
        cleanup_all(current_ip)
        sys.exit(0)
    except Exception as e:
        logger.exception(f"发生未预期错误: {e}")
        cleanup_all(current_ip)
        sys.exit(1)

    cleanup_all(current_ip)
    sys.exit(0)

if __name__ == "__main__":
    main()