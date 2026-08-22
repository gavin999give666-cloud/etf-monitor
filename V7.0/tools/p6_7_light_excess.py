"""
P6.7 light 重优化：两标的各一轮 light-excess 300 trials
=====================================================
- 搜索空间: EXCESS_SEARCH_SPACE (31 维) + benchmark_beating 目标
- 结果导出: runs/{code}/light_excess_results.json

用法:
  python tools/p6_7_light_excess.py             # 两标的 300 trials
  python tools/p6_7_light_excess.py 589800 100  # 单标的自定义试验数
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))


def run(code, n_trials=300, n_jobs=4):
    import config
    config.activate_profile(code)
    from param_optimizer import run_light_optimization

    out = os.path.join(ROOT, 'runs', code, 'light_excess_results.json')
    results = run_light_optimization(n_trials=n_trials, n_jobs=n_jobs,
                                     output_path=out, verbose=True,
                                     objective='benchmark_beating')
    if not results:
        print(f"[{code}] 无有效结果！")
        return None
    best = results[0]
    print(f"\n[{code}] ========== P6.7 light-excess 最佳参数组 ==========")
    print(f"  Obj={best.objective:.2f} | 超额={best.excess_return*100:+.2f}pp | "
          f"策略收益={best.strategy_return*100:.2f}% | 年化={best.annualized_return*100:.2f}%")
    print(f"  回撤={best.max_drawdown*100:.2f}% | 基准回撤={best.benchmark_max_drawdown*100:.2f}% | "
          f"交易={best.total_trades} | Sharpe={best.sharpe_ratio:.2f}")
    return best


if __name__ == '__main__':
    codes = sys.argv[1:-1] or ['589800', '563360']
    n = int(sys.argv[-1]) if len(sys.argv) > 1 and sys.argv[-1].isdigit() else 300
    for code in codes:
        run(code, n_trials=n, n_jobs=4)