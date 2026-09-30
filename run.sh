#!/usr/bin/env bash
# 一条本地启动命令：建库、生成训练数据与泄漏/借调/报废样例、启动 Web。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8080}"
HOST="${HOST:-127.0.0.1}"
SETTLE="${SETTLE_SECONDS:-900}"

if [[ "${1:-}" == "--reset" ]]; then
  shift
  EXTRA="--reset"
else
  EXTRA=""
fi

echo "==============================================="
echo " 消防站空气呼吸器气瓶充装与检测系统"
echo " http://${HOST}:${PORT}"
echo " 静置复测时长: ${SETTLE}s（后台线程自动提醒）"
echo " 样例: 泄漏封存 / 紧急借调 / 报废确认 / 终压不足"
echo "==============================================="

exec python3 -m app --host "$HOST" --port "$PORT" --settle-seconds "$SETTLE" $EXTRA
