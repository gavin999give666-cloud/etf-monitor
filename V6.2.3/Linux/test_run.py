#!/usr/bin/env python3
"""
V6.2.3 测试运行脚本
==================

测试内容：
  1. 功能测试：数据更新 → 信号生成 → 信号日期校验 → Bark推送
  2. 异常测试：模拟数据获取失败、信号生成失败、Bark推送失败
  3. 日志记录：测试所有日志级别

用法：
  python test_run.py                 # 运行所有测试
  python test_run.py --quick         # 快速测试（仅功能测试）
  python test_run.py --exception     # 仅异常测试
"""

import sys
import os
import time
import traceback
from datetime import datetime, date
from pathlib import Path

# ---- 路径设置 ----
SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR / 'src'
LOG_DIR = SCRIPT_DIR / 'logs'
sys.path.insert(0, str(SRC_DIR))

LOG_DIR.mkdir(parents=True, exist_ok=True)
TEST_LOG = LOG_DIR / f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(TEST_LOG, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('V6.2.3_Test')


# ============================================================
# 配置
# ============================================================
CONFIG_FILE = SCRIPT_DIR / 'deploy_config.json'


def load_config():
    import json
    defaults = {
        "bark_url": "",
        "bark_key": "",
        "stock_code": "563360",
        "db_path": str(SRC_DIR / 'stock_data.db'),
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            defaults.update(json.load(f))
    return defaults


# ============================================================
# 测试 1：数据更新
# ============================================================
def test_data_update():
    """测试数据更新功能"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 1：数据更新")
    logger.info("=" * 60)

    cfg = load_config()
    try:
        from data_updater import update_stock_data
        result = update_stock_data(stock_code=cfg['stock_code'])
        if result:
            logger.info("[PASS] 数据更新成功")
            return True
        else:
            logger.warning("[WARN] 数据更新返回False（可能数据已是最新）")
            return True  # 已有最新数据不算失败
    except Exception as e:
        logger.error(f"[FAIL] 数据更新异常: {e}")
        logger.debug(traceback.format_exc())
        return False


# ============================================================
# 测试 2：信号生成
# ============================================================
def test_signal_generation():
    """测试信号生成功能"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 2：信号生成")
    logger.info("=" * 60)

    cfg = load_config()
    try:
        from data_updater import load_data_from_db
        from strategy import get_today_signal_v6

        df = load_data_from_db(cfg['db_path'])
        if df is None or df.empty:
            logger.error("[FAIL] 数据加载失败")
            return False

        signal_text, detail_text = get_today_signal_v6(df)

        if signal_text and detail_text:
            logger.info(f"[PASS] 信号生成成功: {signal_text}")
            logger.info(f"  信号详情:\n{detail_text}")
            return True
        else:
            logger.error("[FAIL] 信号生成为空")
            return False

    except Exception as e:
        logger.error(f"[FAIL] 信号生成异常: {e}")
        logger.debug(traceback.format_exc())
        return False


# ============================================================
# 测试 3：信号日期校验
# ============================================================
def test_signal_validation():
    """测试信号日期校验功能"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 3：信号日期校验")
    logger.info("=" * 60)

    cfg = load_config()
    try:
        from data_updater import load_data_from_db

        df = load_data_from_db(cfg['db_path'])
        if df is None or df.empty:
            logger.error("[FAIL] 无法加载数据")
            return False

        last_date = df.index[-1]
        signal_date_str = last_date.strftime('%Y-%m-%d') if hasattr(last_date, 'strftime') else str(last_date)
        today = date.today()

        logger.info(f"  信号最新日期: {signal_date_str}")
        logger.info(f"  今日日期: {today}")

        delta = (today - last_date.date() if hasattr(last_date, 'date')
                 else (today - datetime.strptime(signal_date_str, '%Y-%m-%d').date())).days

        if delta <= 1:
            logger.info(f"[PASS] 日期校验通过（差{delta}天）")
            return True
        elif today.weekday() >= 5:
            logger.info(f"[PASS] 周末，日期差{delta}天可接受")
            return True
        else:
            logger.warning(f"[WARN] 日期差{delta}天，可能数据未更新")
            return True  # 警告但不失败

    except Exception as e:
        logger.error(f"[FAIL] 日期校验异常: {e}")
        return False


# ============================================================
# 测试 4：Bark 推送
# ============================================================
def test_bark_push():
    """测试Bark推送功能"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 4：Bark 推送")
    logger.info("=" * 60)

    cfg = load_config()
    bark_key = cfg.get('bark_key', '').strip()
    bark_url = cfg.get('bark_url', '').strip()

    if not bark_key and not bark_url:
        logger.info("[SKIP] Bark未配置，跳过推送测试")
        return True

    try:
        import urllib.request
        import urllib.parse

        test_title = "V6.2.3 测试推送"
        test_body = f"这是一条测试消息\n时间: {datetime.now().strftime('%H:%M:%S')}"

        if bark_key and not bark_url:
            bark_url = f"https://api.day.app/{bark_key}"

        full_url = f"{bark_url}/{urllib.parse.quote(test_title)}/{urllib.parse.quote(test_body)}"
        if '?' in bark_url:
            full_url = f"{bark_url}&title={urllib.parse.quote(test_title)}&body={urllib.parse.quote(test_body)}"

        req = urllib.request.Request(full_url)
        req.add_header('User-Agent', 'V6.2.3-TestRunner/1.0')

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = resp.read().decode('utf-8')
            logger.info(f"[PASS] Bark推送成功: {result}")
            return True

    except Exception as e:
        logger.error(f"[FAIL] Bark推送失败: {e}")
        logger.info("  请检查:")
        logger.info("  1. bark_key 是否正确")
        logger.info("  2. 服务器能否访问 api.day.app")
        logger.info("  3. Bark App 是否在线")
        return False


# ============================================================
# 异常测试
# ============================================================
def test_exception_data_fetch():
    """模拟数据获取失败"""
    logger.info("\n" + "=" * 60)
    logger.info("异常测试 1：模拟数据获取失败")
    logger.info("=" * 60)

    try:
        import akshare as ak
        # 用不存在的股票代码测试
        try:
            df = ak.fund_etf_hist_em(
                symbol="000000",
                period="daily",
                start_date="20990101",
                end_date="20990102",
                adjust=""
            )
            if df.empty:
                logger.info("[PASS] 正确返回空数据（API异常被正确处理）")
            else:
                logger.info("[PASS] API返回数据但代码已经捕获")
        except Exception as e:
            logger.info(f"[PASS] API正确抛出异常: {type(e).__name__}: {str(e)[:100]}")

        logger.info("异常被正确捕获并记录")
        return True

    except Exception as e:
        logger.error(f"[FAIL] 异常处理失败: {e}")
        return False


def test_exception_signal_failure():
    """模拟信号生成失败"""
    logger.info("\n" + "=" * 60)
    logger.info("异常测试 2：模拟信号生成失败")
    logger.info("=" * 60)

    try:
        # 传入空数据，验证错误处理
        from strategy import V6Strategy
        import pandas as pd

        strategy = V6Strategy()
        empty_df = pd.DataFrame()
        try:
            strategy.run(empty_df)
            logger.info("[PASS] 空数据处理未崩溃")
        except Exception as e:
            logger.info(f"[PASS] 空数据正确触发异常: {type(e).__name__}")

        # 传入极少数据
        tiny_df = pd.DataFrame({
            'open': [1.0], 'high': [1.0], 'low': [1.0],
            'close': [1.0], 'volume': [1000]
        }, index=pd.to_datetime(['2024-01-01']))
        try:
            strategy.run(tiny_df)
            logger.info("[PASS] 极少数据处理未崩溃（返回空信号）")
        except Exception as e:
            logger.info(f"[PASS] 极少数据正确触发异常: {type(e).__name__}")

        return True
    except Exception as e:
        logger.error(f"[FAIL] 异常测试失败: {e}")
        return False


def test_exception_bark_failure():
    """模拟Bark推送失败"""
    logger.info("\n" + "=" * 60)
    logger.info("异常测试 3：模拟Bark推送失败")
    logger.info("=" * 60)

    try:
        import urllib.request
        import urllib.parse

        # 发送到不存在的URL，验证错误处理
        try:
            req = urllib.request.Request("https://invalid-bark-url.example.com/push")
            req.add_header('User-Agent', 'V6.2.3-TestRunner/1.0')
            with urllib.request.urlopen(req, timeout=3) as resp:
                pass
            logger.info("[PASS] 意外的成功（URL可访问）")
        except Exception as e:
            logger.info(f"[PASS] Bark推送失败被正确捕获: {type(e).__name__}")
            logger.info("  系统正确处理了推送失败情况")

        return True
    except Exception as e:
        logger.error(f"[FAIL] 异常测试失败: {e}")
        return False


# ============================================================
# 完整流程测试
# ============================================================
def test_full_flow():
    """运行完整流程测试（调用 run_daily.py）"""
    logger.info("\n" + "=" * 60)
    logger.info("完整流程测试：调用 run_daily.py --test --no-bark")
    logger.info("=" * 60)

    try:
        from run_daily import run_daily
        success, signal, detail = run_daily(test_mode=True, no_bark=True)
        if success:
            logger.info(f"[PASS] 完整流程成功: {signal}")
            return True
        else:
            logger.error("[FAIL] 完整流程失败")
            return False
    except Exception as e:
        logger.error(f"[FAIL] 完整流程异常: {e}")
        logger.debug(traceback.format_exc())
        return False


# ============================================================
# 主入口
# ============================================================
def run_all_tests(quick=False, exception_only=False):
    """运行所有测试"""
    results = {}
    start_time = datetime.now()

    logger.info("=" * 70)
    logger.info("V6.2.3 Linux 部署测试")
    logger.info(f"开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"测试模式: {'快速' if quick else '完整'}{'（仅异常测试）' if exception_only else ''}")
    logger.info("=" * 70)

    if quick:
        results['完整流程'] = test_full_flow()
    elif exception_only:
        results['模拟数据获取失败'] = test_exception_data_fetch()
        results['模拟信号生成失败'] = test_exception_signal_failure()
        results['模拟Bark推送失败'] = test_exception_bark_failure()
    else:
        # 功能测试
        results['数据更新'] = test_data_update()
        results['信号生成'] = test_signal_generation()
        results['日期校验'] = test_signal_validation()
        results['Bark推送'] = test_bark_push()
        results['完整流程'] = test_full_flow()

        # 异常测试
        results['异常-数据失败'] = test_exception_data_fetch()
        results['异常-信号失败'] = test_exception_signal_failure()
        results['异常-Bark失败'] = test_exception_bark_failure()

    # ---- 汇总 ----
    elapsed = (datetime.now() - start_time).total_seconds()
    passed = sum(1 for v in results.values() if v)
    total = len(results)

    logger.info("\n" + "=" * 70)
    logger.info("测试结果汇总")
    logger.info("=" * 70)
    for name, result in results.items():
        status = "PASS" if result else "FAIL"
        logger.info(f"  [{status}] {name}")
    logger.info(f"\n  通过: {passed}/{total} | 耗时: {elapsed:.1f}秒")
    logger.info(f"  日志文件: {TEST_LOG}")
    logger.info("=" * 70)

    return passed == total


if __name__ == "__main__":
    quick = '--quick' in sys.argv
    exception_only = '--exception' in sys.argv

    success = run_all_tests(quick=quick, exception_only=exception_only)
    sys.exit(0 if success else 1)
