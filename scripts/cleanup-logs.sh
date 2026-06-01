#!/usr/bin/env bash
# 清理项目 logs/ 目录中超过 7 天的日志文件
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LOG_DIR="${PROJECT_ROOT}/logs"
RETENTION_DAYS=7
DRY_RUN="${DRY_RUN:-0}"

if [[ ! -d "${LOG_DIR}" ]]; then
  echo "[cleanup-logs] 日志目录不存在，跳过: ${LOG_DIR}"
  exit 0
fi

echo "[cleanup-logs] 目录=${LOG_DIR} 保留天数=${RETENTION_DAYS}"

find "${LOG_DIR}" -type f -mtime "+${RETENTION_DAYS}" -print | while read -r f; do
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "[dry-run] 将删除: ${f}"
  else
    rm -f "${f}"
    echo "已删除: ${f}"
  fi
done

echo "[cleanup-logs] 完成"
