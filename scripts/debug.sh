#!/bin/bash
# ptpilot 本地调试脚本:docker 命令直接构建镜像,再启动 docker compose
# 与生产部署(直接拉 GHCR 镜像)不同,本脚本用于本地代码修改后的快速调试
# 用法: ./scripts/debug.sh
set -e
cd "$(dirname "$0")/.."

# 镜像名与 docker-compose.yml 中 image 保持一致(部署版 compose 不含 build 段)
IMAGE="ghcr.io/cnstark/ptpilot:latest"

echo "==> [1/2] 构建本地镜像 (docker build -t ${IMAGE} .)"
docker build -t "${IMAGE}" .

echo "==> [2/2] 启动容器 (docker compose up -d)"
docker compose up -d

echo ""
echo "==> 容器状态"
docker compose ps

echo ""
echo "查看日志: docker compose logs -f ptpilot"
