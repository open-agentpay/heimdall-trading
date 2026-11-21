#!/usr/bin/env bash
# ==============================================================================
# Heimdall AI Quant Trading Platform - 生产级 Docker 服务编排与运维脚本
# ==============================================================================
# 用法:
#   ./deploy.sh pull      # 从远端 Hub 拉取最新生产镜像 (后端 + 前端)
#   ./deploy.sh up        # 启动容器栈 (PostgreSQL + 后端 + 前端)
#   ./deploy.sh update    # 一键滚动更新 (拉取新镜像 -> 重启服务 -> 清理孤立镜像)
#   ./deploy.sh down      # 停止并移除容器（完整保留数据库与金库数据卷）
#   ./deploy.sh restart   # 重启容器栈
#   ./deploy.sh logs      # 实时查看日志 (例: ./deploy.sh logs backend)
#   ./deploy.sh status    # 查看各容器运行状态与健康检查
#   ./deploy.sh backup    # 导出 PostgreSQL 逻辑备份至 ./backups/
# ==============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  echo "[错误] 未检测到 Docker Compose，请先安装 Docker Desktop 或 docker-compose-plugin。"
  exit 1
fi

ensure_docker_running() {
  if ! docker info >/dev/null 2>&1; then
    echo "[错误] Docker 守护进程未运行，请先启动 Docker 后再重试。"
    exit 1
  fi
}

ensure_env_file() {
  if [ ! -f ".env" ]; then
    echo "[部署初始化] 未找到 .env，正在从 .env.example 生成配置模板..."
    cp .env.example .env
    echo "[提示] 请先在 .env 中设置 POSTGRES_PASSWORD、HEIMDALL_MASTER_KEY 与 HEIMDALL_CORS_ORIGINS 后再次运行 ./deploy.sh up"
    exit 1
  fi
}

cmd_pull() {
  ensure_docker_running
  ensure_env_file
  echo ">>> [Heimdall 部署] 正在从远程 Hub 拉取最新镜像..."
  $COMPOSE_CMD pull
  echo "✅ 镜像拉取完成！"
}

cmd_up() {
  ensure_docker_running
  ensure_env_file
  echo ">>> [Heimdall 部署] 正在启动生产服务栈..."
  $COMPOSE_CMD up -d "$@"
  $COMPOSE_CMD ps
  WEB_PORT_VAL="$(grep -E '^WEB_PORT=' .env 2>/dev/null | cut -d '=' -f2 || true)"
  WEB_PORT_VAL="${WEB_PORT_VAL:-80}"
  echo ""
  echo "=============================================================================="
  echo "🚀 Heimdall Trading Platform 服务栈已运行！"
  echo "  - Web 控制台入口 : http://localhost:${WEB_PORT_VAL}"
  echo "  - 后端健康检查   : http://localhost:${WEB_PORT_VAL}/health"
  echo "  - 实时日志查看   : ./deploy.sh logs"
  echo "=============================================================================="
}

cmd_update() {
  ensure_docker_running
  ensure_env_file
  echo ">>> [Heimdall 部署] 正在执行生产滚动更新..."
  $COMPOSE_CMD pull
  $COMPOSE_CMD up -d --remove-orphans
  echo ">>> [Heimdall 部署] 正在清理废弃镜像层..."
  docker image prune -f >/dev/null 2>&1 || true
  $COMPOSE_CMD ps
  echo "✅ 生产服务已成功更新至最新镜像！"
}

cmd_down() {
  ensure_docker_running
  echo ">>> [Heimdall 部署] 正在停止并移除容器（数据卷将完整保留）..."
  $COMPOSE_CMD down
}

cmd_restart() {
  ensure_docker_running
  ensure_env_file
  echo ">>> [Heimdall 部署] 正在重启前后端与数据库服务..."
  $COMPOSE_CMD restart
  $COMPOSE_CMD ps
}

cmd_logs() {
  ensure_docker_running
  shift || true
  $COMPOSE_CMD logs -f --tail=200 "$@"
}

cmd_status() {
  ensure_docker_running
  $COMPOSE_CMD ps
}

cmd_backup() {
  ensure_docker_running
  mkdir -p backups
  TS="$(date +%Y%m%d_%H%M%S)"
  PG_USER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d '=' -f2 || true)"
  PG_DB="$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | cut -d '=' -f2 || true)"
  PG_USER="${PG_USER:-heimdall}"
  PG_DB="${PG_DB:-heimdall}"
  OUT_FILE="backups/heimdall_pg_${TS}.sql.gz"
  echo ">>> [Heimdall 备份] 正在导出 PostgreSQL 数据库至 ${OUT_FILE}..."
  $COMPOSE_CMD exec -T postgres pg_dump -U "$PG_USER" "$PG_DB" | gzip > "$OUT_FILE"
  echo ">>> [Heimdall 备份] 备份完成: ${OUT_FILE}"
}

ACTION="${1:-up}"
case "$ACTION" in
  pull) cmd_pull ;;
  up) shift || true; cmd_up "$@" ;;
  update) cmd_update ;;
  down) cmd_down ;;
  restart) cmd_restart ;;
  logs) cmd_logs "$@" ;;
  status) cmd_status ;;
  backup) cmd_backup ;;
  *)
    echo "未知指令: $ACTION"
    echo "支持指令: ./deploy.sh [pull|up|update|down|restart|logs|status|backup]"
    exit 1
    ;;
esac
