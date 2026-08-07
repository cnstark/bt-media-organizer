#!/bin/bash
# ptpilot 重启脚本:stop → rm → build → up
# 用法: ./restart.sh
set -e
cd "$(dirname "$0")"

echo "==> [1/4] 停止并删除旧容器 (docker compose down = stop + rm)"
docker compose down --remove-orphans

echo "==> [2/4] 重新构建镜像 (docker compose build)"
docker compose build

echo "==> [3/4] 启动容器"
docker compose up -d

echo "==> [4/4] 容器状态"
docker compose ps

echo ""
echo "查看日志: docker compose logs -f ptpilot"
