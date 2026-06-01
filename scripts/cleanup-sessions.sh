#!/usr/bin/env bash
# 清理项目 .eido/workspaces/ 下超过 7 天未修改的会话目录（含 uploads/outputs 中间文件）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

WORKSPACES_DIR="${PROJECT_ROOT}/.eido/workspaces"
RETENTION_DAYS=7
DRY_RUN="${DRY_RUN:-0}"

SESSION_ID_RE='^[A-Za-z0-9_-]{1,64}$'

if [[ ! -d "${WORKSPACES_DIR}" ]]; then
  echo "[cleanup-sessions] 工作区不存在，跳过: ${WORKSPACES_DIR}"
  exit 0
fi

echo "[cleanup-sessions] 目录=${WORKSPACES_DIR} 保留天数=${RETENTION_DAYS}"

find "${WORKSPACES_DIR}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -print | while read -r dir; do
  name="$(basename "${dir}")"
  if [[ ! "${name}" =~ ${SESSION_ID_RE} ]]; then
    echo "跳过非会话目录: ${dir}"
    continue
  fi
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "[dry-run] 将删除会话: ${dir}"
  else
    rm -rf "${dir}"
    echo "已删除会话: ${name}"
  fi
done

echo "[cleanup-sessions] 完成"
