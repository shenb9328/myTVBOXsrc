#!/usr/bin/env bash
# 一键推送当前本地点播源到局域网泰捷电视盒子 (192.168.0.245)
BOX_IP="192.168.0.245"
BOX_PORT="9978"
NAS_IP="192.168.0.116"
VOD_URL="http://${NAS_IP}:5888/vod.json"

echo "正在向电视盒子 (${BOX_IP}:${BOX_PORT}) 推送专线配置..."
RESP=$(curl -s -X POST -d "do=api&url=${VOD_URL}" "http://${BOX_IP}:${BOX_PORT}/action")

if [ "$RESP" = "ok" ]; then
    echo "✓ 推送成功! 电视盒子已同步加载专线配置: ${VOD_URL}"
else
    echo "✗ 推送结果: ${RESP}"
    echo "请检查电视端是否已开启影视仓 (端口 9978)"
fi
