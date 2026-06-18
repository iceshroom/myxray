#!/bin/bash
#===============================================================================
# Xray + IPv6 自动更换 一键部署脚本 (动态获取 IPv6)
# 功能：安装Xray (VLESS+REALITY) + 部署流量监控服务 + 生成客户端TUN配置
# 适用系统：Ubuntu / Debian
# 运行方式：sudo bash deploy.sh
#===============================================================================

set -e

# --- 颜色 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# --- 可配置参数 (可根据需要修改) ---
GITHUB_REPO="iceshroom/myxray"            # 你的GitHub仓库
PYTHON_SCRIPT_NAME="ip_rotator.py"              # Python脚本文件名
XRAY_PORT=443                                   # Xray监听端口
REALITY_DEST="www.microsoft.com:443"            # Reality伪装目标
SERVER_NAME="www.microsoft.com" # SNI列表
SERVERS_JSON='["www.microsoft.com"]'
SHORT_ID=""                                     # 留空自动生成
CLIENT_CONFIG_PATH="$HOME/myxray/client-config.json"          # 客户端配置输出路径

# --- 检查root ---
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}错误：此脚本必须以root权限运行！${NC}" 
   exit 1
fi

# --- 获取网络信息 ---
DEFAULT_IFACE=$(ip route | grep default | awk '{print $5}' | head -n1)
if [[ -z "$DEFAULT_IFACE" ]]; then
    echo -e "${RED}无法获取默认网卡，请手动设置 INTERFACE 变量。${NC}"
    exit 1
fi
echo -e "${GREEN}默认网卡: $DEFAULT_IFACE${NC}"

# 获取第一个 scope global 的 IPv6 地址（公网IPv6）
IPV6_ADDR=$(ip -6 addr show dev "$DEFAULT_IFACE" | grep -E "inet6.*scope global" | awk '{print $2}' | cut -d'/' -f1 | head -n1)
if [[ -z "$IPV6_ADDR" ]]; then
    echo -e "${RED}错误：未找到公网 IPv6 地址，请确认网卡已配置 IPv6。${NC}"
    exit 1
fi
echo -e "${GREEN}当前 IPv6 地址: $IPV6_ADDR${NC}"

# 提取 /64 前缀（去掉最后一段，末尾加 ::）
IPV6_PREFIX=$(echo "$IPV6_ADDR" | sed 's/:[^:]*$//')":"
echo -e "${GREEN}IPv6 前缀: ${IPV6_PREFIX}${NC}"

# --- 1. 更新系统并安装依赖 ---
echo -e "${GREEN}>>> 更新系统并安装依赖...${NC}"
apt update -y && apt upgrade -y
apt install -y curl wget unzip python3 python3-pip git jq

# --- 2. 安装 Xray-core ---
echo -e "${GREEN}>>> 安装 Xray-core...${NC}"
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install

# --- 3. 生成 UUID 和 Reality 密钥 ---
echo -e "${GREEN}>>> 生成 UUID 和 Reality 密钥...${NC}"
UUID=$(/usr/local/bin/xray uuid)
KEYS=$(/usr/local/bin/xray x25519)
PRIVATE_KEY=$(echo "$KEYS" | head -n1 | awk '{print $2}')
PUBLIC_KEY=$(echo "$KEYS" | tail -n2 | head -n1 | awk '{print $3}')
if [[ -z "$SHORT_ID" ]]; then
    SHORT_ID=$(openssl rand -hex 8)
fi

# 执行命令并捕获完整输出（请将 ... 替换为实际参数）
output=$(xray vlessenc)

# 提取 ML-KEM-768 的解密密钥
decryption_key=$(echo "$output" | awk '/Authentication: ML-KEM-768, Post-Quantum/{p=1} p && /^"decryption":/{gsub(/^"decryption": "/,""); gsub(/"$/,""); print; exit}')

# 提取 ML-KEM-768 的加密密钥
encryption_key=$(echo "$output" | awk '/Authentication: ML-KEM-768, Post-Quantum/{p=1} p && /^"encryption":/{gsub(/^"encryption": "/,""); gsub(/"$/,""); print; exit}')

# 查看结果
echo -e "${YELLOW}UUID: $UUID${NC}"
echo -e "${YELLOW}Reality Private Key: $PRIVATE_KEY${NC}"
echo -e "${YELLOW}Reality Public Key: $PUBLIC_KEY${NC}"
echo -e "${YELLOW}Vless Decryption: $decryption_key${NC}"
echo -e "${YELLOW}Vless Encryption: $encryption_key${NC}"
echo -e "${YELLOW}Short ID: $SHORT_ID${NC}"

# --- 4. 配置 Xray 服务端 (VLESS+REALITY) ---
echo -e "${GREEN}>>> 配置 Xray 服务端...${NC}"
cat > /usr/local/etc/xray/config.json << EOF
{
  	"log": { "loglevel": "warning" },
	"api": {
		"tag": "api",
		"listen": "127.0.0.1:8081",
		"services": [
			"HandlerService",
			"LoggerService",
			"StatsService",
			"RoutingService"
		]
	},
	"inbounds": [
		{
			"listen": "0.0.0.0",
			"port": $XRAY_PORT,
			"protocol": "vless",
			"users": [
				{
					"id": "$UUID",
					"flow": "xtls-rprx-vision",
					"level": 0
				}
			],
			"settings": {
				"clients": [ { "id": "$UUID", "flow": "xtls-rprx-vision" } ],
				"decryption": "$decryption_key"
			},
			"streamSettings": {
				"network": "xhttp",
				"xhttpSettings": {
					"path": "/"
				},
				"security": "reality",
				"realitySettings": {
					"show": false,
					"target": "$REALITY_DEST",
					"xver": 0,
					"serverNames": $SERVERS_JSON,
					"privateKey": "$PRIVATE_KEY",
					"shortIds": ["$SHORT_ID"]
				}
			}
		}
	],
	"outbounds": [
		{
			"tag": "direct",
			"protocol": "freedom",
			"settings": {}
		},
		{
			"tag": "mega_out",
			"protocol": "freedom",
			"settings": { "domainStrategy": "UseIPv6" }
		},
		{
			"tag": "block",
			"protocol": "blackhole",
			"settings": {}
		}
	],
	"routing": {
		"rules": [
			{ "ip": ["geoip:private"], "outboundTag": "block" }
		]
	},
	"stats": {},
	"policy": {
		"levels": { "0": { "statsUserUplink": true, "statsUserDownlink": true } },
		"system": { "statsInboundUplink": true, "statsInboundDownlink": true }
	}
}
EOF

# --- 5. 配置 systemd 服务 ---
echo -e "${GREEN}>>> 配置 systemd 服务...${NC}"
cat > /etc/systemd/system/ip-rotator.service << EOF
[Unit]
Description=Xray IPv6 Rotator Service
After=network.target xray.service
Requires=xray.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/myxray
ExecStart=/usr/bin/python3 /root/myxray/$PYTHON_SCRIPT_NAME
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ip-rotator.service
systemctl start ip-rotator.service
systemctl restart xray

# --- 7. 生成客户端 TUN 配置 (config.json) ---
echo -e "${GREEN}>>> 生成客户端配置文件: $CLIENT_CONFIG_PATH${NC}"
# 获取公网IPv4
SERVER_IP=$(ip -4 -o addr show $(ip -4 route show default | awk 'NR==1{print $5}') | awk '{print $4}' | cut -d/ -f1)  


cat > "$CLIENT_CONFIG_PATH" << EOF
{
	"log": { "loglevel": "info" },
	"inbounds": [
		{
			"protocol": "tun",
			"settings": {
				"name": "xray0",
				"mtu": 1500,
				"gateway": ["10.0.0.1/16", "fc00::1/64"],
				"dns": ["1.1.1.1", "8.8.8.8"],
				"userLevel": 0,
				"autoSystemRoutingTable": ["0.0.0.0/0", "::/0"],
				"autoOutboundsInterface": "enp6s0"
			},
			"sniffing": {
				"enabled": true,
				"destOverride": ["http", "tls", "fakedns"]
			}
		}
  	],
  	"outbounds": [
		{
			"tag": "proxy",
			"protocol": "vless",
			"settings": {
				"address": "$SERVER_IP",
				"port": $XRAY_PORT,
				"id": "$UUID",
				"encryption": "$encryption_key",
				"flow": "xtls-rprx-vision",
				"level": 0
			},
			"streamSettings": {
				"network": "xhttp",
				"xhttpSettings": {
					"path": "/"
				},
				"security": "reality",
				"realitySettings": {
					"serverName": "$SERVER_NAME",
					"fingerprint": "chrome",
					"publicKey": "$PUBLIC_KEY",
					"shortId": "$SHORT_ID"
				}
			}
		},
		{
			"tag": "direct",
			"protocol": "freedom",
			"settings": {}
		},
		{
			"tag": "block",
			"protocol": "blackhole",
			"settings": {
				"response": {
					"type": "none"
				}
      		}
    	}
  	],
  	"routing": {
		"rules": [
			{
				"outboundTag": "direct",
				"ip": ["geoip:private", "$SERVER_IP"]
			},
			{
				"outboundTag": "block",
				"domain": ["geosite:category-ads"]
			}
		]
	}
}
EOF

# --- 9. 输出客户端配置信息 ---
echo -e "\n${GREEN}========== 部署完成！客户端配置 ==========${NC}"
echo -e "服务端地址: $SERVER_IP"
echo -e "端口: $XRAY_PORT"
echo -e "UUID: $UUID"
echo -e "Flow: xtls-rprx-vision"
echo -e "公钥: $PUBLIC_KEY"
echo -e "Short ID: $SHORT_ID"
echo -e "SNI: $SERVER_NAME"
echo -e "\n${YELLOW}客户端配置文件已生成: $CLIENT_CONFIG_PATH${NC}"
echo -e "${YELLOW}可直接复制该文件到客户端 Xray 目录使用。${NC}"
echo -e "\n${GREEN}监控服务状态：${NC}"
systemctl status ip-rotator --no-pager --lines=0

echo -e "\n${GREEN}部署完毕！${NC}"