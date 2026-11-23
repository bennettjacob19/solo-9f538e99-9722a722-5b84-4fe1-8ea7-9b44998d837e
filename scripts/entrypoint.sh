#!/usr/bin/env bash
# 容器入口：首次启动时生成样例仓库，然后启动 Web 服务。
set -euo pipefail

REPO_PATH="${REPO_PATH:-/data/sample-repo}"
WORKTREE_ROOT="${WORKTREE_ROOT:-/data/worktrees}"
PORT="${PORT:-8000}"

mkdir -p "$(dirname "$REPO_PATH")" "$WORKTREE_ROOT"

if [ ! -d "$REPO_PATH/.git" ]; then
  echo "首次启动，生成样例仓库: $REPO_PATH"
  /app/scripts/make_sample_repo.sh "$REPO_PATH"
fi

exec gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 8 app:app
