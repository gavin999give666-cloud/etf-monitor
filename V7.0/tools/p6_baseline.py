"""
P6.1 基线固化 + 交易成本审计
============================
对每个预置标的运行 V6 全套回测，导出完整指标到 runs\{code}\baseline_v6.json，
并实证审计 SLIPPAGE / COMMISSION 是否实际计入交易成本。

成本审计方法：
  同一标的分三次回测：
    A) 正常成本（SLIPPAGE=0.001, COMMISSION=0.0003）  ← 基线
    B) 零成本（SLIPPAGE=0, COMMISSION=0）
    C) 仅滑点（SLIPPAGE=0.001, COMMISSION=0）
    D) 仅佣金（SLIPPAGE=0, COMMISSION=0.0003）
  若 A 的最终收益 < B，且 A < C、A < D，则证明两类成本均实际计入。

用法：
  python tools/p6_baseline.py            # 固化两标的基线 + 成本审计
  python tools/p6_baseline.py --audit    # 仅成本审计（不写基线文件）
退出码：0=全部通过
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))

INSTRUMENTS = ['589800', '563360']

# 成本审计场景：(SLIPPAGE, COMMISSION, 标签)
AUDIT_SCENARIOS = [
    (0.001, 0.0003, '正常成本'),
    (0.0, 0.0, '零成本'),
    (0.001, 0.0, '仅滑点'),
    (0.0, 0.0003, '仅佣金'),
]


def run_backtest(code, slippage, commission):
    """激活 profile → 运行 V6 策略 + 回测，返回 (results, bt)"""
    import config
    config.activate_profile(code)

    # 覆盖成本参数（审计用；基线场景与 profile 一致）
    # 注意：position_manager 通过 `from config import SLIPPAGE` 在导入时绑定常量，
    # 必须同时覆盖 position_manager 模块属性才生效（否则审计无效）。
    config.SLIPPAGE = slippage
    config.COMMISSION = commission
    import position_manager as pm_mod
    pm_mod.SLIPPAGE = slippage
    pm_mod.COMMISSION = commission

    from data_updater import load_data_from_db
    from strategy import V6Strategy
    from backtest import V6Backtest

    df = load_data_from_db()
    if df is None:
        raise RuntimeError(f"{code} 数据加载失败")

    strategy = V6Strategy(use_ml=True)
    signals = strategy.run(df)
    bt = V6Backtest(df, strategy=strategy)
    results = bt.run(signals)
    return results, bt


def collect_baseline(results, bt):
    """从回测结果提取完整基线指标"""
    import config
    return {
        'meta': {
            'version': 'V6.2.3',
            'code': config.STOCK_CODE,
            'name': config.ETF_NAME,
            'backtest_start': str(bt.df.index[0].date()),
            'backtest_end': str(bt.df.index[-1].date()),
            'trading_days': len(bt.daily_equity),
            'data_rows': len(bt.df_orig),
            'generated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
        'performance': {
            'benchmark_return_pct': round(results['benchmark_return'] * 100, 2),
            'strategy_return_pct': round(results['strategy_return'] * 100, 2),
            'excess_return_pct': round(results['excess_return'] * 100, 2),
            'max_drawdown_pct': round(results['max_drawdown'] * 100, 2),
            'annualized_return_pct': round(results['annualized_return'] * 100, 2),
            'volatility_pct': round(results['volatility'] * 100, 2),
            'sharpe_ratio': round(results['sharpe_ratio'], 3),
            'sortino_ratio': round(results.get('sortino_ratio', 0), 3),
            'calmar_ratio': round(results.get('calmar_ratio', 0), 3),
            'profit_factor': round(results.get('profit_factor', 0), 2),
            'win_rate_pct': round(results.get('win_rate', 0) * 100, 1),
            'kelly': round(results.get('kelly', 0), 3),
            'expectancy_pct': round(results.get('expectancy', 0), 2),
            'total_trades': results.get('total_trades', 0),
            'winning_trades': results.get('winning_trades', 0),
            'losing_trades': results.get('losing_trades', 0),
            'avg_hold_days': round(results.get('avg_hold_days', 0), 1),
            'final_equity': round(results.get('final_equity', 0), 2),
            'max_consecutive_wins': results.get('max_consecutive_wins', 0),
            'max_consecutive_losses': results.get('max_consecutive_losses', 0),
        },
        'position_stats': compute_position_stats(bt),
        'config_snapshot': {
            'initial_cash': bt.initial_cash,
            'initial_position': bt.initial_position,
            'max_position': config.MAX_POSITION,
            'min_hold_days': config.MIN_HOLD_DAYS,
            'slippage': config.SLIPPAGE,
            'commission': config.COMMISSION,
        },
    }


def compute_position_stats(bt):
    """统计每日实际仓位分布（验证 [0.70, 0.95] 窄带诊断）"""
    positions = [s['current_position'] for s in bt.daily_signals]
    if not positions:
        return {}
    n = len(positions)
    return {
        'min': round(min(positions), 4),
        'max': round(max(positions), 4),
        'mean': round(sum(positions) / n, 4),
        'pct_ge_90': round(sum(1 for p in positions if p >= 0.90) / n * 100, 1),
        'pct_lt_30': round(sum(1 for p in positions if p < 0.30) / n * 100, 1),
        'pct_lt_70': round(sum(1 for p in positions if p < 0.70) / n * 100, 1),
    }


def audit_costs(code):
    """成本审计：四场景对比最终收益"""
    print(f"\n  [{code}] 成本审计（SLIPPAGE/COMMISSION 是否实际计入）")
    print(f"  {'场景':<10}{'最终资产':>14}{'策略收益':>12}{'交易数':>8}")
    print(f"  {'-'*48}")
    results_map = {}
    for slippage, commission, label in AUDIT_SCENARIOS:
        results, _ = run_backtest(code, slippage, commission)
        results_map[label] = results
        print(f"  {label:<10}{results['final_equity']:>14.2f}"
              f"{results['strategy_return'] * 100:>11.2f}%"
              f"{results['total_trades']:>8}")

    base = results_map['正常成本']['final_equity']
    zero = results_map['零成本']['final_equity']
    slip = results_map['仅滑点']['final_equity']
    comm = results_map['仅佣金']['final_equity']

    checks = {
        '正常成本 < 零成本（成本整体拖累收益）': base < zero,
        '仅滑点 < 零成本（滑点计入）': slip < zero,
        '仅佣金 < 零成本（佣金计入）': comm < zero,
    }
    all_pass = True
    for desc, ok in checks.items():
        print(f"  {'✅' if ok else '❌'} {desc}")
        all_pass = all_pass and ok
    return all_pass


def main():
    only_audit = '--audit' in sys.argv
    all_pass = True

    for code in INSTRUMENTS:
        print("=" * 60)
        print(f"标的: {code}")
        print("=" * 60)

        # 成本审计（正常成本场景即基线）
        ok = audit_costs(code)
        all_pass = all_pass and ok

        if only_audit:
            continue

        # 用正常成本（profile 默认值）重新跑一次固化基线
        results, bt = run_backtest(code, 0.001, 0.0003)
        baseline = collect_baseline(results, bt)

        import config
        out_path = config.runs_path('baseline_v6.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)
        print(f"\n  基线已固化: {out_path}")
        p = baseline['performance']
        print(f"  基准={p['benchmark_return_pct']}% 策略={p['strategy_return_pct']}% "
              f"超额={p['excess_return_pct']}% 回撤={p['max_drawdown_pct']}% "
              f"交易={p['total_trades']}笔")
        ps = baseline['position_stats']
        print(f"  仓位: [{ps['min']}, {ps['max']}] 均值={ps['mean']} "
              f">=90%占{ps['pct_ge_90']}% <70%占{ps['pct_lt_70']}%")

    print("\n" + "=" * 60)
    if all_pass:
        print("P6.1 完成：成本审计通过（SLIPPAGE/COMMISSION 均已计入）")
    else:
        print("P6.1 警告：成本审计存在异常，请人工核查 position_manager.py")
    print("=" * 60)
    sys.exit(0 if all_pass else 1)


if __name__ == '__main__':
    main()
