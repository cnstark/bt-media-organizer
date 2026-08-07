#!/bin/bash
# ptpilot 重启脚本(部署版):stop → rm → pull → up
# 镜像由 GitHub Actions 在打 tag 时自动构建推送(ghcr.io/cnstark/ptpilot)
# 用法: ./restart.sh
set -e
cd "$(dirname "$0")"

echo "==> [1/4] 停止并删除旧容器 (docker compose down = stop + rm)"
docker compose down --remove-orphans

echo "==> [2/4] 拉取最新镜像 (docker compose pull)"
docker compose pull

echo "==> [3/4] 启动容器"
docker compose up -d

echo "==> [4/4] 容器状态"
docker compose ps

echo ""
echo "查看日志: docker compose logs -f ptpilot"
