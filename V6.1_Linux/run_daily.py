#!/usr/bin/env python3
"""
V6.1 每日自动运行脚本 —— Linux 服务器版本
============================================

功能流程：
  1. 数据更新（带重试）
  2. 信号生成
  3. 信号日期校验（必须为当日信号）
  4. 若校验失败 → 重试数据更新（最多3轮）
  5. Bark 推送当日信号

用法：
  python run_daily.py                    # 正常每日运行
  python run_daily.py --test             # 测试模式（强制运行，跳过日期校验）
  python run_daily.py --no-bark          # 不发送Bark推送
"""

import sys
import os
import json
import time
import traceback
from datetime import datetime, date
from pathlib import Path

# ---- 路径设置 ----
SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR / 'src'
LOG_DIR = SCRIPT_DIR / 'logs'
CONFIG_FILE = SCRIPT_DIR / 'deploy_config.json'

sys.path.insert(0, str(SRC_DIR))

# ---- 日志配置 ----
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.log"

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('V6.1_Daily')


# ============================================================
# 配置加载
# ============================================================
def load_deploy_config():
    """加载部署配置文件，不存在则使用默认值"""
    defaults = {
        "bark_url": "",           # Bark推送URL（必填）
        "bark_key": "",           # Bark Key
        "stock_code": "563360",
        "db_path": str(SRC_DIR / 'stock_data.db'),
        "max_data_retries": 3,    # 数据更新最大重试次数
        "max_signal_rounds": 3,   # 信号校验失败最大重试轮次
        "retry_wait_seconds": 30, # 重试等待时间
        "timeout_seconds": 120,   # 单次操作超时
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            defaults.update(cfg)
    else:
        logger.warning(f"配置文件不存在: {CONFIG_FILE}，使用默认配置")
    return defaults


# ============================================================
# 工具函数
# ============================================================
def is_today(date_obj):
    """判断日期是否为今天"""
    if isinstance(date_obj, str):
        date_obj = datetime.strptime(date_obj, '%Y-%m-%d').date()
    elif isinstance(date_obj, datetime):
        date_obj = date_obj.date()
    return date_obj == date.today()


def is_weekday():
    """判断今天是否为工作日（周一到周五）"""
    return date.today().weekday() < 5


# ============================================================
# Step 1: 数据更新（带重试）
# ============================================================
def step_update_data(cfg, attempt=0):
    """
    更新股票数据

    如果数据库已有今天的数据，直接跳过（视为成功）。
    否则尝试从API获取最新数据。

    Returns:
        (success: bool, message: str)
    """
    max_retries = cfg['max_data_retries']
    attempt_info = f"[第{attempt + 1}/{max_retries}次]" if attempt > 0 else ""

    try:
        # 先检查数据库中是否已有最新数据
        import sqlite3
        db_path = cfg['db_path']
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT MAX(date) FROM stock_data")
            last_date = cursor.fetchone()[0]
            conn.close()
            if last_date:
                today_str = date.today().strftime('%Y-%m-%d')
                if last_date >= today_str:
                    logger.info(f"数据已是最新 ({last_date})，跳过更新")
                    return True, f"数据已是最新 ({last_date})"

        from data_updater import update_stock_data
        logger.info(f"数据更新开始 {attempt_info}")

        success = update_stock_data(stock_code=cfg['stock_code'])
        if success:
            logger.info("数据更新成功")
            return True, "数据更新成功"
        else:
            # 再次检查：可能API失败但数据库已有今天数据
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                cursor = conn.execute("SELECT MAX(date) FROM stock_data")
                last_date = cursor.fetchone()[0]
                conn.close()
                today_str = date.today().strftime('%Y-%m-%d')
                if last_date and last_date >= today_str:
                    logger.info(f"API调用失败但数据已是最新 ({last_date})，视为成功")
                    return True, f"数据已是最新 ({last_date})"

            logger.warning("数据更新返回False（可能无新数据或API不可用）")
            return False, "数据更新未获取到新数据"

    except Exception as e:
        logger.error(f"数据更新异常: {type(e).__name__}: {e}")
        logger.debug(traceback.format_exc())
        return False, f"数据更新异常: {e}"


# ============================================================
# Step 2: 信号生成
# ============================================================
def step_generate_signal(cfg):
    """
    生成当日信号

    Returns:
        (success: bool, signal_text: str, detail_text: str, signal_date: str)
    """
    try:
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6

        db_path = cfg['db_path']
        if not os.path.exists(db_path):
            logger.error(f"数据库文件不存在: {db_path}")
            return False, "数据库文件不存在", "", ""

        df = load_data_from_db(db_path)
        if df is None or df.empty:
            logger.error("数据加载失败或为空")
            return False, "数据加载失败", "", ""

        last_date = df.index[-1]
        signal_date_str = last_date.strftime('%Y-%m-%d') if hasattr(last_date, 'strftime') else str(last_date)

        signal_text, detail_text = get_today_signal_v6(df)

        logger.info(f"信号生成成功 | 数据最新日期: {signal_date_str} | 信号摘要: {signal_text}")
        return True, signal_text, detail_text, signal_date_str

    except Exception as e:
        logger.error(f"信号生成异常: {type(e).__name__}: {e}")
        logger.debug(traceback.format_exc())
        return False, f"信号生成异常: {e}", "", ""


# ============================================================
# Step 3: 信号日期校验
# ============================================================
def step_validate_signal(signal_date_str, cfg):
    """
    校验信号日期是否为当日最新

    非交易日时允许信号日期为最近交易日。

    Returns:
        (valid: bool, message: str)
    """
    try:
        signal_date = datetime.strptime(signal_date_str, '%Y-%m-%d').date()
    except:
        logger.error(f"无法解析信号日期: {signal_date_str}")
        return False, f"信号日期格式异常: {signal_date_str}"

    today = date.today()

    if signal_date == today:
        logger.info(f"信号日期校验通过: {signal_date} == {today}")
        return True, "信号为当日最新"

    # 非交易日：允许信号为最近交易日
    if today.weekday() >= 5:
        logger.info(f"今天是周末({today})，信号日期 {signal_date} 为最近交易日，视为通过")
        return True, f"非交易日，信号日期为最近交易日 {signal_date}"

    # 日期差1天且今天刚开始（可能是盘前）
    delta = (today - signal_date).days
    if delta == 1:
        logger.info(f"信号日期差1天(可能盘前)，视为通过: {signal_date}")
        return True, f"信号日期 {signal_date}（盘前允许差1天）"

    logger.warning(f"信号日期校验失败: 信号{signal_date} != 今日{today} (差{delta}天)")
    return False, f"信号日期不匹配: 信号{signal_date} != 今日{today}"


# ============================================================
# Step 4: Bark 推送
# ============================================================
def step_bark_push(signal_text, detail_text, signal_date_str, cfg):
    """
    通过 Bark 推送当日信号

    Returns:
        (success: bool, message: str)
    """
    bark_url = cfg.get('bark_url', '').strip()
    bark_key = cfg.get('bark_key', '').strip()

    if not bark_url and not bark_key:
        logger.warning("Bark未配置（bark_url和bark_key均为空），跳过推送")
        return False, "Bark未配置"

    # 构建Bark请求
    if bark_key and not bark_url:
        bark_url = f"https://api.day.app/{bark_key}"

    title = f"A500 ETF V6.1 信号 {signal_date_str}"
    body = f"{signal_text}\n\n{detail_text[:500]}"  # 截断过长内容

    try:
        import urllib.request
        import urllib.parse

        full_url = f"{bark_url}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
        if '?' in bark_url:
            # 已有参数，用 & 拼接
            full_url = f"{bark_url}&title={urllib.parse.quote(title)}&body={urllib.parse.quote(body)}"

        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'V6.1-DailyRunner/1.0')

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = resp.read().decode('utf-8')
            logger.info(f"Bark推送成功: {result}")
            return True, f"推送成功: {result}"

    except Exception as e:
        logger.error(f"Bark推送失败: {type(e).__name__}: {e}")
        return False, f"Bark推送失败: {e}"


# ============================================================
# 主流程
# ============================================================
def run_daily(test_mode=False, no_bark=False):
    """
    每日主流程

    Args:
        test_mode: 测试模式（跳过日期校验）
        no_bark: 不发送Bark推送
    """
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info(f"V6.1 每日任务启动 | 时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"测试模式: {test_mode} | Bark推送: {not no_bark}")
    logger.info("=" * 60)

    cfg = load_deploy_config()

    if no_bark:
        cfg['bark_url'] = ''
        cfg['bark_key'] = ''

    # ---- Round 1: 数据更新 + 信号生成 + 校验 ----
    signal_ok = False
    signal_text = ""
    detail_text = ""
    signal_date_str = ""

    for round_num in range(1, cfg['max_signal_rounds'] + 1):
        logger.info(f"\n{'─' * 40}")
        logger.info(f"第 {round_num}/{cfg['max_signal_rounds']} 轮尝试")
        logger.info(f"{'─' * 40}")

        # Step 1: 数据更新
        data_ok = False
        for data_attempt in range(cfg['max_data_retries']):
            data_ok, data_msg = step_update_data(cfg, attempt=data_attempt)
            if data_ok:
                break
            if data_attempt < cfg['max_data_retries'] - 1:
                logger.info(f"等待 {cfg['retry_wait_seconds']} 秒后重试数据更新...")
                time.sleep(cfg['retry_wait_seconds'])

        if not data_ok:
            logger.warning(f"第{round_num}轮数据更新失败，等待后重试整轮...")
            if round_num < cfg['max_signal_rounds']:
                time.sleep(cfg['retry_wait_seconds'])
                continue
            else:
                logger.error(f"所有{cfg['max_signal_rounds']}轮数据更新均失败")
                break

        # Step 2: 信号生成
        sig_ok, signal_text, detail_text, signal_date_str = step_generate_signal(cfg)
        if not sig_ok:
            logger.warning(f"第{round_num}轮信号生成失败")
            if round_num < cfg['max_signal_rounds']:
                time.sleep(cfg['retry_wait_seconds'])
                continue
            else:
                logger.error("所有轮次信号生成均失败")
                break

        # Step 3: 日期校验
        if test_mode:
            logger.info("测试模式：跳过日期校验")
            signal_ok = True
            break

        valid, valid_msg = step_validate_signal(signal_date_str, cfg)
        if valid:
            signal_ok = True
            break
        else:
            logger.warning(f"第{round_num}轮信号日期校验失败: {valid_msg}")
            if round_num < cfg['max_signal_rounds']:
                # 删除数据库最后一条记录后重试（强制重新获取）
                logger.info("强制重新获取数据...")
                try:
                    db_path = cfg['db_path']
                    import sqlite3
                    conn = sqlite3.connect(db_path)
                    conn.execute("DELETE FROM stock_data WHERE date = (SELECT MAX(date) FROM stock_data)")
                    conn.commit()
                    conn.close()
                    logger.info("已删除最新一天数据，准备重新获取")
                except Exception as e:
                    logger.warning(f"删除最新数据失败: {e}")
                time.sleep(cfg['retry_wait_seconds'])
            else:
                logger.error("所有轮次信号日期校验均失败")
                signal_ok = False

    # ---- 汇总结果 ----
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"\n{'=' * 60}")
    logger.info(f"任务结束 | 耗时: {elapsed:.1f}秒")
    logger.info(f"最终状态: {'成功' if signal_ok else '失败'}")
    logger.info(f"{'=' * 60}")

    if signal_ok:
        logger.info(f"信号摘要: {signal_text}")
        logger.info(f"信号日期: {signal_date_str}")

        # Step 4: Bark 推送
        if not no_bark and cfg.get('bark_url') or cfg.get('bark_key'):
            push_ok, push_msg = step_bark_push(signal_text, detail_text, signal_date_str, cfg)
            logger.info(f"Bark推送: {'成功' if push_ok else '失败'} - {push_msg}")
        else:
            logger.info("Bark推送已跳过")

        return True, signal_text, detail_text
    else:
        logger.error("任务失败：未能获取有效当日信号")
        return False, "", ""


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    test_mode = '--test' in sys.argv
    no_bark = '--no-bark' in sys.argv

    success, signal, detail = run_daily(test_mode=test_mode, no_bark=no_bark)

    if success:
        print(f"\n{'=' * 60}")
        print(f"最终信号: {signal}")
        print(f"{'=' * 60}")
        sys.exit(0)
    else:
        print(f"\n{'=' * 60}")
        print(f"任务失败，请检查日志: {LOG_FILE}")
        print(f"{'=' * 60}")
        sys.exit(1)
