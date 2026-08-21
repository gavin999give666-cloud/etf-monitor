"""
P6.5 三层合成接线验证
=====================
验证目标：
1. STRATEGY_MODE='V6' 时回测与 baseline_v6.json 完全一致
   （证明 P6.5 接线未破坏 V6 分支，回退路径始终可用）
2. STRATEGY_MODE='V7' 时两标的全段回测出数：
   - 仓位序列出现 <30% 低仓时段（旧版 589800 pct_lt_30=0.0，V7 必须突破）
   - L3 风控动作（止损/止盈/熔断）有实际触发
   - Overheat/BottomFishing 相位有实际触发
3. 输出 V7 绩效与仓位统计，供 P6.8 验收矩阵对照

用法：
  python tools/p6_5_verify.py
退出码：0=全部通过；1=存在失败
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))

INSTRUMENTS = ['589800', '563360']

PASS = 0
FAIL = 0
FAILURES = []


def check(desc, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {desc}")
    else:
        FAIL += 1
        FAILURES.append(desc)
        print(f"  ❌ {desc}  {detail}")


def run_backtest(code, mode='V7'):
    """指定 STRATEGY_MODE 下运行回测，返回 (results, bt)"""
    import config
    config.activate_profile(code)
    config.STRATEGY_MODE = mode

    from data_updater import load_data_from_db
    from strategy import V6Strategy
    from backtest import V6Backtest

    df = load_data_from_db()
    strategy = V6Strategy(use_ml=True)
    signals = strategy.run(df)
    bt = V6Backtest(df, strategy=strategy)
    results = bt.run(signals)
    return results, bt


def position_stats(bt):
    """从 daily_signals 统计仓位序列"""
    positions = [s.get('current_position', 0) for s in bt.daily_signals]
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


def risk_action_stats(bt):
    """统计 L3 风控动作类型计数"""
    counts = {}
    for s in bt.daily_signals:
        for act in s.get('risk_actions', []):
            t = act.get('type', 'unknown')
            counts[t] = counts.get(t, 0) + 1
    return counts


def phase_stats(bt):
    """统计相位触发天数"""
    overheat = sum(1 for s in bt.daily_signals if s.get('phase', {}).get('overheat'))
    bottom = sum(1 for s in bt.daily_signals if s.get('phase', {}).get('bottom_fishing'))
    return {'overheat_days': overheat, 'bottom_fishing_days': bottom}


def test_v6_regression():
    """验证 1：V6 分支回归（接线未破坏旧路径）"""
    print("\n[1] V6 分支回归（STRATEGY_MODE='V6' 与 baseline_v6.json 对比）")
    for code in INSTRUMENTS:
        print(f"\n  [{code}]")
        results, _ = run_backtest(code, mode='V6')

        import config
        with open(config.runs_path('baseline_v6.json'), encoding='utf-8') as f:
            baseline = json.load(f)['performance']

        mapping = {
            'strategy_return_pct': results['strategy_return'] * 100,
            'benchmark_return_pct': results['benchmark_return'] * 100,
            'excess_return_pct': results['excess_return'] * 100,
            'max_drawdown_pct': results['max_drawdown'] * 100,
            'annualized_return_pct': results['annualized_return'] * 100,
            'total_trades': results['total_trades'],
            'win_rate_pct': round(results['win_rate'] * 100, 1),
            'final_equity': results['final_equity'],
        }
        errors = []
        for key, expected in baseline.items():
            if key not in mapping:
                continue
            actual = mapping[key]
            if abs(actual - expected) > 0.01:
                errors.append(f"{key}: baseline={expected} vs V6分支={round(actual, 2)}")
        check(f"{code} V6 分支与基线一致", not errors, '; '.join(errors[:5]))


def test_v7_output():
    """验证 2：V7 三层架构出数 + 仓位/风控/相位统计"""
    print("\n[2] V7 三层架构全段回测（STRATEGY_MODE='V7'）")
    for code in INSTRUMENTS:
        print(f"\n  [{code}]")
        results, bt = run_backtest(code, mode='V7')

        ps = position_stats(bt)
        ra = risk_action_stats(bt)
        ph = phase_stats(bt)

        print(f"    策略收益: {results['strategy_return']*100:+.2f}% | "
              f"基准: {results['benchmark_return']*100:+.2f}% | "
              f"超额: {results['excess_return']*100:+.2f}pp")
        print(f"    最大回撤: {results['max_drawdown']*100:.2f}% | "
              f"交易数: {results['total_trades']} | 胜率: {results['win_rate']*100:.1f}%")
        print(f"    仓位: min={ps.get('min')} max={ps.get('max')} mean={ps.get('mean')} "
              f"pct<30%={ps.get('pct_lt_30')}% pct<70%={ps.get('pct_lt_70')}%")
        print(f"    L3风控动作: {ra if ra else '无'}")
        print(f"    相位触发: Overheat={ph['overheat_days']}天 BottomFishing={ph['bottom_fishing_days']}天")

        # 关键断言：V7 必须出现 <30% 低仓时段（旧版 589800 为 0.0%）
        check(f"{code} V7 出现 <30% 低仓时段", ps.get('pct_lt_30', 0) > 0,
              f"pct_lt_30={ps.get('pct_lt_30')}%")
        # L3 风控必须有动作（止损/止盈/熔断至少一类）
        check(f"{code} L3 风控动作触发", len(ra) > 0, f"actions={ra}")
        # 相位至少一类触发
        check(f"{code} 相位检测触发", ph['overheat_days'] + ph['bottom_fishing_days'] > 0,
              f"overheat={ph['overheat_days']} bottom={ph['bottom_fishing_days']}")


def main():
    test_v6_regression()
    test_v7_output()

    print("\n" + "=" * 60)
    print(f"P6.5 三层合成接线验证: {PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("失败项:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("P6.5 接线验证全部通过 ✅")
    print("=" * 60)


if __name__ == '__main__':
    main()
