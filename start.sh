#!/usr/bin/env bash
# 一条命令启动 SCBA 气瓶充装与检测系统：
#   建库 -> 写入训练数据并生成 泄漏/借调/报废 样例 -> 启动 Web 与后台复测/提醒引擎
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# 仅使用 Python 标准库，无需 pip 安装。
PY="${PYTHON:-python3}"

echo "============================================================"
echo " 消防 SCBA · 空气呼吸器气瓶充装与检测系统"
echo " 访问地址: http://${HOST}:${PORT}"
echo " 首次启动自动生成：泄漏复测 / 紧急借调 / 瓶阀报废 样例"
echo " 后台引擎：静置复测到期自动执行，检验到期自动提醒"
echo " 按 Ctrl+C 退出"
echo "============================================================"

exec "$PY" app.py --host "$HOST" --port "$PORT" "$@"
