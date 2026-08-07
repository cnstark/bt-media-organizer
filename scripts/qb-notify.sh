#!/bin/sh
# qBittorrent 下载完成回调 → ptpilot 自动整理
# 由 qB "Run external program on torrent completion" 调用:
#   参数 1 = %F(内容路径),参数 2 = %I(info-hash,可选,用于回传打标签)
# qB 命令示例: /path/qb-notify.sh "%F" "%I"
# 注意: qB 与 ptpilot 容器需挂载相同路径,不做路径映射,保证地址一致

# 令牌来源(二选一):环境变量 PTPILOT_TOKEN > 令牌文件 /config/bt-media-token
# 不要硬编码 token 到脚本(仓库公开,会泄露)
TOKEN="${PTPILOT_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -r /config/bt-media-token ]; then
    TOKEN="$(cat /config/bt-media-token 2>/dev/null | tr -d '[:space:]')"
fi
if [ -z "$TOKEN" ]; then
    echo "qb-notify: 未设置 PTPILOT_TOKEN 且无 /config/bt-media-token,跳过" >&2
    exit 0
fi

# ptpilot 服务地址(建议经环境变量 PTPILOT_API 注入,勿在仓库提交私有地址)
# 默认值按本机部署环境修改;qB 容器内 127.0.0.1 指向 qB 自身,务必设为 ptpilot 可达地址
API="${PTPILOT_API:-http://10.8.8.2:8900}/api/v1/transfer"
# 下载器名称(须与服务器 config.yaml 的 downloaders[].name 一致,用于回传打标签)
DL="${PTPILOT_DOWNLOADER:-qb}"

if [ -z "$1" ]; then
    exit 0
fi

# 不做路径映射:qB 与 ptpilot 挂载相同路径,直接透传
P="$1"
H="$2"

# 给文件系统一点稳定时间(校验/落盘)
sleep 3

# 带 hash 时一并回传,服务端在整理成功后直接打「已整理」标签;
# 不带 hash 则退化为仅按路径整理,标签由轮询兜底补打(最长 60s)
if [ -n "$H" ]; then
    BODY="{\"path\":\"$P\",\"hash\":\"$H\",\"downloader\":\"$DL\"}"
else
    BODY="{\"path\":\"$P\"}"
fi

# 回调 ptpilot(幂等,已整理过会自动跳过;失败也静默,由轮询兜底)
curl -s -m 300 -X POST "$API?token=$TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "$BODY" > /dev/null 2>&1

exit 0
