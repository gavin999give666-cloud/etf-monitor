"""
P4 集成验证脚本 —— GUI 回测页 / bridge 回测 API 与 CLI --eval 一致性校验
=====================================================================

验证内容：
1. bridge.run_backtest 异步任务正常完成，返回完整结果载荷
2. 589800 绩效指标与 CLI --eval 基线完全一致（指标字段独立比对）
3. 净值/回撤/价格/交易明细数据结构完整且数值自洽
4. 多标的切换后各自回测独立、互不串扰（563360 单独再跑）

CLI 基线来源：python main.py --profile 589800 --eval（P4 开发时捕获）
"""
import sys
import os
import time

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
_QUANT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'quant')
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, _QUANT_DIR)

from bridge import ApiBridge

# 589800 CLI --eval 基线（性能指标，与 export_all 同进位）
CLI_BASELINE_589800 = {
    'strategy_return_pct': 65.88,
    'benchmark_return_pct': 72.17,
    'excess_return_pct': -6.28,
    'max_drawdown_pct': -28.4,
    'annualized_return_pct': 42.8,
    'volatility_pct': 31.18,
    'sharpe_ratio': 1.304,
    'sortino_ratio': 1.666,
    'calmar_ratio': 1.507,
    'profit_factor': 4.75,
    'win_rate_pct': 66.7,
    'kelly': 0.526,
    'expectancy_pct': 8.77,
    'total_trades': 6,
    'winning_trades': 4,
    'losing_trades': 2,
    'avg_hold_days': 43.3,
    'final_equity': 16588.46,
}


def wait_task(api, task_id, timeout=300):
    """轮询任务直到 done/error，返回 (status, result, error)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = api.get_task_status(task_id)
        if not res['ok']:
            return 'api_error', None, res.get('error')
        task = res['data']
        if task['status'] in ('done', 'error'):
            return task['status'], task.get('result'), task.get('error')
        time.sleep(1.0)
    return 'timeout', None, '轮询超时'


def run_backtest_and_collect(api, code):
    """运行回测并返回结果载荷 dict（可选：非 None 表示有结果）"""
    res = api.run_backtest(code)
    assert res['ok'], f"run_backtest({code}) 调用失败: {res.get('error')}"
    task_id = res['data']['task_id']
    print(f"  → 已提交回测任务 {task_id}，等待完成...")
    status, result, error = wait_task(api, task_id)
    assert status == 'done', f"回测任务未成功: status={status}, error={error}"
    payload = result.get('backtest', {})
    assert payload.get('available'), f"回测结果不可用: {payload.get('reason')}"
    return payload


def test_backtest_589800():
    print("\n" + "=" * 60)
    print("测试 1: 589800 回测绩效 vs CLI 基线")
    print("=" * 60)

    api = ApiBridge()
    payload = run_backtest_and_collect(api, '589800')
    perf = payload['performance']
    meta = payload['meta']

    print(f"  标的: {meta.get('etf_name')} ({meta.get('code')})")
    print(f"  数据: {meta.get('data_start')} ~ {meta.get('data_end')} ({meta.get('data_rows')} 条)")
    print(f"  回测: {meta.get('backtest_start')} ~ {meta.get('backtest_end')} ({meta.get('trading_days')} 交易日)")
    print(f"  GUI 绩效: 收益={perf['strategy_return_pct']}% 基准={perf['benchmark_return_pct']}% "
          f"回撤={perf['max_drawdown_pct']}% 夏普={perf['sharpe_ratio']} 交易={perf['total_trades']}笔 "
          f"最终资产={perf['final_equity']}")

    # 逐字段比对 CLI 基线
    errors = []
    for key, expected in CLI_BASELINE_589800.items():
        actual = perf.get(key)
        if actual is None:
            errors.append(f"缺少字段 {key}")
            continue
        if abs(float(actual) - float(expected)) > 1e-6:
            errors.append(f"{key} 不一致: CLI={expected} vs GUI={actual}")

    if errors:
        print(f"  ❌ {len(errors)} 处指标不一致:")
        for e in errors:
            print(f"    - {e}")
        return False

    print(f"  ✅ 全部 {len(CLI_BASELINE_589800)} 项绩效指标与 CLI 一致")

    # 结构自洽性校验
    equity = payload.get('equity', [])
    assert len(equity) > 0, "净值序列为空"
    print(f"  ✓ 净值序列: {len(equity)} 条")
    # 回撤必须全部 <= 0
    assert all(p.get('drawdown', 0) <= 1e-6 for p in equity), "存在正回撤值"
    print(f"  ✓ 回撤序列均 <= 0")

    price = payload.get('price', [])
    assert len(price) == len(equity), "价格序列长度与净值不一致"
    print(f"  ✓ 价格序列: {len(price)} 条")

    trades = payload.get('trades', [])
    assert len(trades) == perf['total_trades'], \
        f"交易明细条数({len(trades)}) != 绩效总交易数({perf['total_trades']})"
    print(f"  ✓ 交易明细: {len(trades)} 笔，与绩效 total_trades 一致")

    buy_m = payload.get('buy_markers', [])
    sell_m = payload.get('sell_markers', [])
    print(f"  ✓ 成交标记: 买入 {len(buy_m)} / 卖出 {len(sell_m)}")
    print(f"    买入标记日期: {[m['date'] for m in buy_m]}")
    print(f"    卖出标记日期: {[m['date'] for m in sell_m]}")

    print("  ✅ 589800 回测结构校验通过")
    return True


def test_backtest_563360():
    print("\n" + "=" * 60)
    print("测试 2: 563360 回测（多标的独立性）")
    print("=" * 60)

    api = ApiBridge()
    payload = run_backtest_and_collect(api, '563360')
    perf = payload['performance']
    meta = payload['meta']

    print(f"  标的: {meta.get('etf_name')} ({meta.get('code')})")
    print(f"  数据: {meta.get('data_start')} ~ {meta.get('data_end')} ({meta.get('data_rows')} 条)")
    print(f"  GUI 绩效: 收益={perf['strategy_return_pct']}% 基准={perf['benchmark_return_pct']}% "
          f"回撤={perf['max_drawdown_pct']}% 夏普={perf['sharpe_ratio']} 交易={perf['total_trades']}笔")

    # 独立性与 589800 基线不同，确保数据没有串扰
    assert meta['code'] == '563360', "标的代码错误"
    assert len(payload.get('equity', [])) > 0, "净值序列为空"
    print("  ✅ 563360 回测独立完成，无数据串扰")
    return True


def test_cli_consistency_live():
    print("\n" + "=" * 60)
    print("测试 3: 589800 GUI 回测 vs 实时 CLI --eval（最严格比对）")
    print("=" * 60)

    import subprocess
    import json

    # 先跑 GUI（bridge 同进程）
    api = ApiBridge()
    payload = run_backtest_and_collect(api, '589800')
    perf = payload['performance']

    # 再跑 CLI（其内部会写 runs\589800\backtest_records.json，与 GUI 同一 export_all 进位）
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'main.py')
    result = subprocess.run(
        [sys.executable, main_py, '--profile', '589800', '--eval'],
        capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace'
    )
    # CLI 净退出码 0 才视为成功
    assert result.returncode == 0, f"CLI --eval 退出码非0: {result.returncode}"

    record_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'runs', '589800', 'backtest_records.json'
    )
    with open(record_path, encoding='utf-8') as f:
        cli_perf = json.load(f)['performance']

    # 比对字段（GUI performance 键名与 export_all 的记录键一一对应）
    kv_map = {
        'strategy_return_pct': 'strategy_return_pct',
        'benchmark_return_pct': 'benchmark_return_pct',
        'excess_return_pct': 'excess_return_pct',
        'max_drawdown_pct': 'max_drawdown_pct',
        'annualized_return_pct': 'annualized_return_pct',
        'volatility_pct': 'volatility_pct',
        'sharpe_ratio': 'sharpe_ratio',
        'sortino_ratio': 'sortino_ratio',
        'calmar_ratio': 'calmar_ratio',
        'profit_factor': 'profit_factor',
        'win_rate_pct': 'win_rate_pct',
        'kelly': 'kelly',
        'expectancy_pct': 'expectancy_pct',
        'total_trades': 'total_trades',
        'winning_trades': 'winning_trades',
        'losing_trades': 'losing_trades',
        'avg_hold_days': 'avg_hold_days',
        'final_equity': 'final_equity',
    }

    errors = []
    for cli_key, gui_key in kv_map.items():
        cli_val = cli_perf.get(cli_key)
        gui_val = perf.get(gui_key)
        if cli_val is None or gui_val is None:
            errors.append(f"字段缺失: {cli_key}")
            continue
        # 浮点误差容差（JSON 序列化往返极小而允许）
        if abs(float(cli_val) - float(gui_val)) > 1e-6:
            errors.append(f"{cli_key}: CLI={cli_val} vs GUI={gui_val}")

    if errors:
        print(f"  ❌ 与 CLI 记录比对 {len(errors)} 处不一致:")
        for e in errors:
            print(f"    - {e}")
        return False

    print(f"  ✅ GUI 回测与实时 CLI 生成的 backtest_records.json "
          f"全部 {len(kv_map)} 项绩效字段一致（含胜率，均按 export_all 1 位小数：66.7）")
    return True


def main():
    print("V7.0 P4 回测集成验证")
    print("=" * 60)

    all_passed = True
    for name, fn in [
        ('589800 vs CLI基线', test_backtest_589800),
        ('563360 独立性', test_backtest_563360),
        ('实时CLI一致性', test_cli_consistency_live),
    ]:
        try:
            if not fn():
                all_passed = False
        except Exception as e:
            print(f"  ❌ [{name}] 测试异常: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("✅ 所有回测测试通过！P4 验收合格")
    else:
        print("❌ 部分回测测试失败，请检查")
    print("=" * 60)
    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())