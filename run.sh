#!/usr/bin/env bash
# 一条命令：建库（含多经纪人冲突样例）并启动服务。
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"

python3 -m app.seed --reset
echo
echo "------------------------------------------------------------"
echo " 铜印房产业务系统  http://localhost:${PORT}"
echo " 多经纪人冲突样例：首页 → 佣金争议 → #1"
echo " 主管身份（裁决用）：右上角切换为 陈岚 或 孙岩"
echo "------------------------------------------------------------"
exec python3 -m app.server "${PORT}"
