#!/usr/bin/env python3
"""
IPv6 Traffic Monitor & Address Rotator (Pure Address Swap)
- Sets IPv6 preference for outgoing connections
- Monitors IPv6 download traffic via ip6tables
- Rotates IPv6 address by ADD NEW + DEL OLD (without touching routing table)
"""

import subprocess
import time
import ipaddress
import random
import sys
import signal
import logging
import atexit
import os

# ---------- Configuration ----------
THRESHOLD = 2 * 1024 ** 3          # 2 GB (bytes)
CHECK_INTERVAL = 10                # seconds
# -----------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ipv6_monitor')

# Global state
current_addr = None      # e.g. "2001:db8:1::1234/64"
gateway = None
iface = None
last_count = None
total_traffic = 0
running = True


def setup_ipv6_preference():
    """Make IPv6 preferred over IPv4 for outgoing connections."""
    # Enable IPv6 (just in case)
    subprocess.run(['sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=0'],
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run(['sysctl', '-w', 'net.ipv6.conf.default.disable_ipv6=0'],
                   stderr=subprocess.DEVNULL, check=False)

    # Lower priority of IPv4-mapped addresses in gai.conf
    gai_file = '/etc/gai.conf'
    try:
        with open(gai_file, 'r') as f:
            content = f.read()
        if 'precedence ::ffff:0:0/96' not in content:
            with open(gai_file, 'a') as f:
                f.write('\n# Added by ipv6_monitor\nprecedence ::ffff:0:0/96  10\n')
            logger.info("Updated /etc/gai.conf to prefer IPv6")
    except Exception as e:
        logger.warning("Could not modify /etc/gai.conf: %s", e)

    # Disable temporary addresses to keep our address management clean
    iface = get_default_interface()
    if iface:
        subprocess.run(
            ['sysctl', '-w', f'net.ipv6.conf.{iface}.use_tempaddr=0'],
            stderr=subprocess.DEVNULL, check=False
        )


def get_default_interface():
    """Return the interface of the default IPv6 route."""
    try:
        out = subprocess.check_output(['ip', '-6', 'route', 'show', 'default'],
                                      text=True)
    except subprocess.CalledProcessError:
        logger.error("No default IPv6 route found")
        sys.exit(1)
    lines = out.strip().splitlines()
    if not lines:
        logger.error("No default IPv6 route")
        sys.exit(1)
    parts = lines[0].split()
    if 'dev' in parts:
        idx = parts.index('dev')
        if idx + 1 < len(parts):
            return parts[idx + 1]
    logger.error("Could not parse interface from default route: %s", lines[0])
    sys.exit(1)


def get_default_gateway_and_src():
    """Return (gateway, source_ip, interface) from the default route."""
    out = subprocess.check_output(['ip', '-6', 'route', 'show', 'default'],
                                  text=True)
    line = out.strip().splitlines()[0]
    parts = line.split()
    gateway = None
    src = None
    iface = None
    if 'via' in parts:
        idx = parts.index('via')
        if idx + 1 < len(parts):
            gateway = parts[idx + 1]
    if 'dev' in parts:
        idx = parts.index('dev')
        if idx + 1 < len(parts):
            iface = parts[idx + 1]
    if 'src' in parts:
        idx = parts.index('src')
        if idx + 1 < len(parts):
            src = parts[idx + 1]
    return gateway, src, iface


def get_global_ipv6_addresses(iface):
    """Return list of global IPv6 addresses (with prefix) on the interface."""
    addrs = []
    try:
        out = subprocess.check_output(
            ['ip', '-6', 'addr', 'show', 'dev', iface, 'scope', 'global'],
            text=True
        )
    except subprocess.CalledProcessError:
        return addrs
    for line in out.splitlines():
        if 'inet6' in line:
            parts = line.strip().split()
            # parts: ['inet6', '2001:db8:1::1234/64', 'scope', 'global', ...]
            if len(parts) >= 2:
                addrs.append(parts[1])
    return addrs


def setup_iptables():
    """Create ip6tables chain to count IPv6 download traffic."""
    # Create chain (ignore if exists)
    subprocess.run(['ip6tables', '-N', 'IPV6_DOWNLOAD'],
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run(['ip6tables', '-F', 'IPV6_DOWNLOAD'], check=False)
    subprocess.run(['ip6tables', '-A', 'IPV6_DOWNLOAD', '-j', 'RETURN'],
                   check=True)

    # Remove any old rule and insert at top of INPUT
    subprocess.run(['ip6tables', '-D', 'INPUT', '-j', 'IPV6_DOWNLOAD'],
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run(['ip6tables', '-I', 'INPUT', '-j', 'IPV6_DOWNLOAD'],
                   check=True)
    logger.info("iptables rules installed")


def cleanup_iptables():
    """Remove the ip6tables rules."""
    subprocess.run(['ip6tables', '-D', 'INPUT', '-j', 'IPV6_DOWNLOAD'],
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run(['ip6tables', '-F', 'IPV6_DOWNLOAD'], check=False)
    subprocess.run(['ip6tables', '-X', 'IPV6_DOWNLOAD'], check=False)
    logger.info("iptables rules removed")


def get_download_bytes():
    """Read the current byte count from the IPV6_DOWNLOAD chain."""
    try:
        out = subprocess.check_output(
            ['ip6tables', '-L', 'IPV6_DOWNLOAD', '-v', '-n', '-x'],
            text=True
        )
    except subprocess.CalledProcessError:
        return 0
    for line in out.splitlines():
        if 'RETURN' in line and 'all' in line:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    pass
    return 0


def generate_new_ipv6_address(existing_addrs, prefix_len=64):
    """Generate a new IPv6 address from the /64 prefix of existing_addrs."""
    if not existing_addrs:
        raise ValueError("No existing IPv6 addresses to derive prefix")
    net = ipaddress.IPv6Network(existing_addrs[0], strict=False)
    while True:
        host = random.getrandbits(64)
        new_addr = net.network_address + host
        new_str = f"{new_addr}/{net.prefixlen}"
        if new_str not in existing_addrs:
            return new_str


def replace_ipv6_address():
    """
    Rotate the IPv6 address by adding a new one and deleting the old one.
    Does NOT touch the routing table (relies on kernel's source address selection).
    """
    global current_addr, iface, total_traffic

    # 1. Get current list of addresses
    all_addrs = get_global_ipv6_addresses(iface)
    if not all_addrs:
        logger.error("No global IPv6 addresses found on %s, cannot rotate", iface)
        return False

    # 2. Generate a new unique address
    new_addr = generate_new_ipv6_address(all_addrs)
    logger.info("Generated new address: %s", new_addr)

    # 3. Add the new address (with nodad to skip DAD and avoid 'tentative' state)
    try:
        result = subprocess.run(
            ['ip', '-6', 'addr', 'add', new_addr, 'dev', iface, 'nodad'],
            capture_output=True, text=True, check=True
        )
        logger.info("Successfully added new address: %s", new_addr)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to add new address: %s", e.stderr)
        return False

    # 4. Delete the old address (if it exists)
    old_addr = current_addr
    if old_addr:
        try:
            subprocess.run(
                ['ip', '-6', 'addr', 'del', old_addr, 'dev', iface],
                capture_output=True, text=True, check=True
            )
            logger.info("Successfully deleted old address: %s", old_addr)
        except subprocess.CalledProcessError as e:
            # This is not fatal, but we log it. The old address might have been
            # removed by something else, or has dependencies.
            logger.warning("Failed to delete old address %s: %s", old_addr, e.stderr)

    # 5. Update global state and reset traffic counter
    current_addr = new_addr
    total_traffic = 0
    logger.info("Address rotation completed. New managed address: %s", current_addr)
    return True


def signal_handler(sig, frame):
    global running
    logger.info("Received signal %d, exiting...", sig)
    running = False


def main():
    global current_addr, gateway, iface, last_count, total_traffic, running

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # 1. Prefer IPv6
    setup_ipv6_preference()

    # 2. Get default route details
    gateway, src, iface = get_default_gateway_and_src()
    if iface is None or gateway is None:
        logger.error("Could not determine default interface or gateway")
        sys.exit(1)
    logger.info("Default interface: %s, gateway: %s", iface, gateway)

    # 3. CRITICAL: Remove the 'src' pinning from the default route if it exists.
    #    This allows the kernel to automatically use the new IP after we delete the old one.
    if src:
        logger.info("Removing 'src %s' pinning from default route...", src)
        try:
            subprocess.run(
                ['ip', '-6', 'route', 'replace', 'default', 'via', gateway, 'dev', iface],
                check=True, capture_output=True, text=True
            )
            logger.info("Default route updated (src constraint removed).")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to update default route: %s", e.stderr)
            sys.exit(1)
    else:
        logger.info("Default route has no 'src' constraint. Good to go.")

    # 4. Get current global IPv6 addresses
    addrs = get_global_ipv6_addresses(iface)
    if not addrs:
        logger.error("No global IPv6 address found on %s", iface)
        sys.exit(1)
    logger.info("Current IPv6 addresses: %s", addrs)

    # Determine the address we will manage (prefer the one used as src in route, else pick first)
    if src and any(src == a.split('/')[0] for a in addrs):
        for a in addrs:
            if a.startswith(src + '/'):
                current_addr = a
                break
    if current_addr is None:
        current_addr = addrs[0]
    logger.info("Managed address: %s", current_addr)

    # 5. Set up traffic counting
    setup_iptables()
    atexit.register(cleanup_iptables)

    last_count = get_download_bytes()
    total_traffic = 0

    logger.info("Monitoring started (threshold = %d bytes, %d GB)",
                THRESHOLD, THRESHOLD // (1024 ** 3))

    # 6. Main loop
    while running:
        time.sleep(CHECK_INTERVAL)
        if not running:
            break

        try:
            current_count = get_download_bytes()
            if last_count is not None:
                delta = current_count - last_count
                if delta < 0:
                    logger.warning("Counter decreased (possibly reset), ignoring")
                    delta = 0
                total_traffic += delta
                logger.debug("Delta: %d bytes, Total: %d bytes (%.2f GB)",
                             delta, total_traffic, total_traffic / (1024 ** 3))

                if total_traffic >= THRESHOLD:
                    logger.info("Threshold reached! Total traffic: %d bytes (%.2f GB)",
                                total_traffic, total_traffic / (1024 ** 3))
                    if replace_ipv6_address():
                        # Reset last_count to current count after rotation
                        last_count = get_download_bytes()
                        logger.info("Counter reset after rotation.")
                    else:
                        logger.error("Address rotation failed. Counter not reset.")
            else:
                last_count = current_count

        except Exception as e:
            logger.exception("Error in monitoring loop: %s", e)

    logger.info("Exiting gracefully")


if __name__ == '__main__':
    main()