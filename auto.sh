#!/bin/bash
apt update -y && apt upgrade -y
apt install -y curl wget unzip python3 python3-pip git jq
git clone --depth=1 https://github.com/iceshroom/myxray /root/myxray
chmod u+x /root/myxray/deploy.sh
sh /root/myxray/deploy.sh