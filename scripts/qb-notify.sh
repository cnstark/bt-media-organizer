#!/bin/sh
# qBittorrent 下载完成回调 → bt-media-organizer 自动整理
# 由 qB "Run external program on torrent completion" 调用,参数 %F = 内容路径
# 用法: qb-notify.sh <content_path>
# 注意: qB 与 bt-media-organizer 容器需挂载相同路径,不做路径映射,保证地址一致

# 令牌来源(二选一):环境变量 BT_MEDIA_TOKEN > 令牌文件 /config/bt-media-token
# 不要硬编码 token 到脚本(仓库公开,会泄露)
TOKEN="${BT_MEDIA_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -r /config/bt-media-token ]; then
    TOKEN="$(cat /config/bt-media-token 2>/dev/null | tr -d '[:space:]')"
fi
if [ -z "$TOKEN" ]; then
    echo "qb-notify: 未设置 BT_MEDIA_TOKEN 且无 /config/bt-media-token,跳过" >&2
    exit 0
fi

# bt-media-organizer 服务地址(建议经环境变量 BT_MEDIA_API 注入,勿在仓库提交私有地址)
# 默认值按本机部署环境修改;qB 容器内 127.0.0.1 指向 qB 自身,务必设为 bt-media-organizer 可达地址
API="${BT_MEDIA_API:-http://10.8.8.2:8900}/api/v1/transfer"

if [ -z "$1" ]; then
    exit 0
fi

# 不做路径映射:qB 与 bt-media-organizer 挂载相同路径,直接透传
P="$1"

# 给文件系统一点稳定时间(校验/落盘)
sleep 3

# 回调 bt-media-organizer(幂等,已整理过会自动跳过;失败也静默,由轮询兜底)
curl -s -m 300 -X POST "$API?token=$TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "{\"path\":\"$P\"}" > /dev/null 2>&1

exit 0
