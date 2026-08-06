#!/bin/sh
# qBittorrent 下载完成回调 → lite-organizer 自动整理
# 由 qB "Run external program on torrent completion" 调用,参数 %F = 内容路径(宿主路径)
# 用法: qb-notify.sh <content_path>

# 令牌来源(二选一):环境变量 LITE_TOKEN > 令牌文件 /config/lite-token
# 不要硬编码 token 到脚本(仓库公开,会泄露)
TOKEN="${LITE_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -r /config/lite-token ]; then
    TOKEN="$(cat /config/lite-token 2>/dev/null | tr -d '[:space:]')"
fi
if [ -z "$TOKEN" ]; then
    echo "qb-notify: 未设置 LITE_TOKEN 且无 /config/lite-token,跳过" >&2
    exit 0
fi

API="http://10.8.8.2:8900/api/v1/transfer"

if [ -z "$1" ]; then
    exit 0
fi

# 宿主路径 → 容器路径映射(/volX/1004/mediaN → /media/mediaN)
case "$1" in
    /vol*/1004/media*)
        P=$(printf '%s' "$1" | sed -E 's|^/vol[0-9]+/1004/media([0-9]+)|/media/media\1|')
        ;;
    *)
        P="$1"
        ;;
esac

# 给文件系统一点稳定时间(校验/落盘)
sleep 3

# 回调 lite-organizer(幂等,已整理过会自动跳过;失败也静默,由轮询兜底)
curl -s -m 300 -X POST "$API?token=$TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "{\"path\":\"$P\"}" > /dev/null 2>&1

exit 0
