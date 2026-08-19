#!/bin/bash
# ============================================================
# V6.1 每日启动脚本 —— crontab 调用入口
# 用法: bash start.sh
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
LOG_DIR="$SCRIPT_DIR/logs"

# 激活虚拟环境
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

# 切换到脚本目录
cd "$SCRIPT_DIR"

# 运行每日任务
python3 "$SCRIPT_DIR/run_daily.py"

# 记录退出码
EXIT_CODE=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') 任务结束，退出码: $EXIT_CODE" >> "$LOG_DIR/cron.log"

exit $EXIT_CODE
