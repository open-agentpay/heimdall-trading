#!/usr/bin/env bash
# ==============================================================================
# Heimdall AI Quant Trading Platform - 本地 Docker 打包并推送远程 Hub 脚本
# ==============================================================================
# 用法:
#   ./docker-publish.sh <REGISTRY> [TAG] [PLATFORM]
#
# 参数说明:
#   REGISTRY : 必填（或通过环境变量 DOCKER_REGISTRY 提供）
#              例如:
#                - Docker Hub: your_username (推送到 your_username/heimdall-*)
#                - 阿里云 ACR: registry.cn-hangzhou.aliyuncs.com/your_ns
#                - GitHub Packages: ghcr.io/your_org
#                - 腾讯云 TCR: ccr.ccs.tencentyun.com/your_ns
#   TAG      : 可选，默认读取当前 Git commit short sha（并同时打 latest 标签）
#   PLATFORM : 可选，默认 linux/amd64（适配绝大部分 Linux 云服务器，避免 Mac M芯片架构冲突）
#
# 运行示例:
#   ./docker-publish.sh myhubuser
#   ./docker-publish.sh myhubuser v1.0.0
#   ./docker-publish.sh registry.cn-hangzhou.aliyuncs.com/myquant v1.0.0 linux/amd64
# ==============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 1. 检查 Docker 守护进程
if ! docker info >/dev/null 2>&1; then
  echo "❌ [错误] Docker 守护进程未启动，请先启动 Docker Desktop 后重试。"
  exit 1
fi

# 2. 解析参数
REGISTRY="${1:-${DOCKER_REGISTRY:-}}"
if [ -z "$REGISTRY" ]; then
  echo "=============================================================================="
  echo "❌ [错误] 未指定远程镜像仓库地址 (REGISTRY)"
  echo "用法: ./docker-publish.sh <REGISTRY> [TAG] [PLATFORM]"
  echo ""
  echo "示例:"
  echo "  Docker Hub:   ./docker-publish.sh mydockerhubuser 1.0.0"
  echo "  阿里云 ACR:   ./docker-publish.sh registry.cn-hangzhou.aliyuncs.com/mynamespace"
  echo "  GitHub GHCR:  ./docker-publish.sh ghcr.io/myorg"
  echo "=============================================================================="
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "prod")"
TAG="${2:-${IMAGE_TAG:-$GIT_SHA}}"
PLATFORM="${3:-${DOCKER_PLATFORM:-linux/amd64}}"

BACKEND_IMAGE="${REGISTRY}/heimdall-backend"
WEB_IMAGE="${REGISTRY}/heimdall-web"

echo "=============================================================================="
echo "🚀 [Heimdall Docker 打包与推送]"
echo "  - 远程仓库前缀 : ${REGISTRY}"
echo "  - 目标架构平台 : ${PLATFORM} (支持从 Mac 本地跨平台打包给 Linux 云服务器)"
echo "  - 镜像版本标签 : ${TAG} 与 latest"
echo "  - 后端目标镜像 : ${BACKEND_IMAGE}:${TAG}"
echo "  - 前端目标镜像 : ${WEB_IMAGE}:${TAG}"
echo "=============================================================================="

# 3. 本地构建后端镜像
echo ""
echo ">>> [1/4] 正在构建后端镜像 (FastAPI + Multi-Agent Engine)..."
docker build \
  --platform "${PLATFORM}" \
  -t "${BACKEND_IMAGE}:${TAG}" \
  -t "${BACKEND_IMAGE}:latest" \
  -f Dockerfile .

# 4. 本地构建前端镜像
echo ""
echo ">>> [2/4] 正在构建前端镜像 (React/Vite + Nginx 反向代理)..."
docker build \
  --platform "${PLATFORM}" \
  -t "${WEB_IMAGE}:${TAG}" \
  -t "${WEB_IMAGE}:latest" \
  -f web/Dockerfile ./web

# 5. 推送后端镜像
echo ""
echo ">>> [3/4] 正在推送后端镜像至远程 Hub..."
echo "    -> 推送 ${BACKEND_IMAGE}:${TAG}"
docker push "${BACKEND_IMAGE}:${TAG}"
echo "    -> 推送 ${BACKEND_IMAGE}:latest"
docker push "${BACKEND_IMAGE}:latest"

# 6. 推送前端镜像
echo ""
echo ">>> [4/4] 正在推送前端镜像至远程 Hub..."
echo "    -> 推送 ${WEB_IMAGE}:${TAG}"
docker push "${WEB_IMAGE}:${TAG}"
echo "    -> 推送 ${WEB_IMAGE}:latest"
docker push "${WEB_IMAGE}:latest"

echo ""
echo "=============================================================================="
echo "✅ 所有镜像构建与推送完成！"
echo "=============================================================================="
echo ""
echo "📋 [服务器端部署指南 - Server Deployment]"
echo "1. 将 docker-compose.yml、deploy.sh 与 .env 上传或同步至服务器工作目录"
echo "2. 在服务器 .env 中指定镜像仓库与版本（或直接作为环境变量传递）："
echo "   ------------------------------------------------------------"
echo "   DOCKER_REGISTRY=${REGISTRY}"
echo "   IMAGE_TAG=${TAG}"
echo "   （三个容器均不映射宿主机端口；对外访问需由加入 heimdall-network 的反代转发到 heimdall-web:80）"
echo "   ------------------------------------------------------------"
echo "3. 在服务器执行一键拉取并启动："
echo "   $ ./deploy.sh pull"
echo "   $ ./deploy.sh up"
echo "   或者直接运行 Docker Compose:"
echo "   $ docker compose pull && docker compose up -d"
echo "=============================================================================="
