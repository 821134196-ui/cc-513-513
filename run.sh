#!/usr/bin/env bash
# 单条本地命令：播种（如需）并启动工作台。
# 用法：./run.sh [--reseed] [--port 8000]
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
RESEED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reseed) RESEED=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [ "$RESEED" = "1" ]; then
  python3 -m app.seed --force
elif [ ! -f data/brokerage.db ]; then
  python3 -m app.seed
fi

exec python3 -m app.server --host 127.0.0.1 --port "$PORT"
