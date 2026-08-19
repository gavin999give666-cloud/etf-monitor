#!/bin/bash
# ============================================================
# V6.2.3 Linux 部署脚本
# 用法: bash setup.sh
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "======================================"
echo "  V6.2.3 A500 ETF 策略系统 - 部署安装"
echo "  目录: $SCRIPT_DIR"
echo "======================================"

# ---- 检查 Python3 ----
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] 未找到 python3，请先安装 Python 3.8+"
    exit 1
fi
PYTHON=$(command -v python3)
echo "[OK] Python: $($PYTHON --version)"

# ---- 创建虚拟环境 ----
VENV_DIR="$SCRIPT_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "[INFO] 创建虚拟环境..."
    $PYTHON -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
echo "[OK] 虚拟环境已激活"

# ---- 安装依赖 ----
echo "[INFO] 安装Python依赖..."
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q
echo "[OK] 依赖安装完成"

# ---- 创建日志目录 ----
mkdir -p "$SCRIPT_DIR/logs"
echo "[OK] 日志目录已创建"

# ---- 初始化数据库 ----
echo "[INFO] 初始化数据（首次获取历史数据）..."
$PYTHON -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR/src')
from data_updater import init_database, full_refresh_data
import config
db_path = '$SCRIPT_DIR/src/stock_data.db'
init_database(db_path)
print('数据库已初始化')
"

# ---- 生成默认配置 ----
CONFIG_FILE="$SCRIPT_DIR/deploy_config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" << 'EOF'
{
    "bark_url": "",
    "bark_key": "",
    "stock_code": "563360",
    "db_path": "src/stock_data.db",
    "max_data_retries": 3,
    "max_signal_rounds": 3,
    "retry_wait_seconds": 30,
    "timeout_seconds": 120
}
EOF
    echo "[OK] 默认配置文件已生成: $CONFIG_FILE"
    echo "     请编辑此文件，填入你的 Bark Key"
else
    echo "[OK] 配置文件已存在"
fi

# ---- 设置执行权限 ----
chmod +x "$SCRIPT_DIR/start.sh"
chmod +x "$SCRIPT_DIR/run_daily.py"
chmod +x "$SCRIPT_DIR/test_run.py"
echo "[OK] 执行权限已设置"

echo ""
echo "======================================"
echo "  部署完成！"
echo "======================================"
echo ""
echo "下一步操作："
echo "  1. 编辑配置文件: nano $CONFIG_FILE"
echo "     (填入你的 Bark Key)"
echo ""
echo "  2. 首次获取数据:"
echo "     source $VENV_DIR/bin/activate"
echo "     python3 $SCRIPT_DIR/src/data_updater.py"
echo ""
echo "  3. 运行测试:"
echo "     source $VENV_DIR/bin/activate"
echo "     python3 $SCRIPT_DIR/test_run.py"
echo ""
echo "  4. 手动运行一次:"
echo "     bash $SCRIPT_DIR/start.sh"
echo ""
echo "  5. 配置定时任务 (每天14:40):"
echo "     crontab -e"
echo "     添加: 40 14 * * 1-5 bash $SCRIPT_DIR/start.sh >> $SCRIPT_DIR/logs/cron.log 2>&1"
echo ""
