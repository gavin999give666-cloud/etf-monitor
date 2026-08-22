"""
P6.8 验收矩阵：按设计文档 §4.2 成功线逐项判定
=====================================================
对指定标的 + 参数组（默认 light_excess_results.json top1，--params-json 可指定 heavy 结果），
逐项判定成功线：
  1. 全段超额（strategy - benchmark）
  2. 最大回撤（≤ 基准一半）
  3. WF OOS 超额（> 0） + IS→OOS 衰减（≤ 60%）
  4. 交易次数（区间）
  5. Bull 主升段低仓占比（< 30%）
输出: runs/{code}/acceptance_report.json

用法:
  python tools/p6_8_acceptance.py              # 两标的，light best
  python tools/p6_8_acceptance.py 589800       # 单标的
  python tools/p6_8_acceptance.py 563360 --params-json runs/563360/heavy_excess_results.json
"""
import json
import os
import sys
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))


def _json_default(o):
    """numpy 标量 → Python 原生类型（回测 results 字段可能为 numpy.float64）"""
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    raise TypeError(f'Object of type {type(o).__name__} is not JSON serializable')

# 设计文档 §4.2 成功线（589800 基准 -28.4% 的一半放宽 1pp；563360 基准 -13.09% 的一半）
SUCCESS_LINES = {
    '589800': {
        'excess_min_pp': 8.0,
        'max_drawdown_max': -0.15,
        'trades_range': (8, 25),
        'wf_oos_excess_min': 0.0,
        'wf_decay_max': 0.60,
        'bull_low_pos_max': 0.30,
    },
    '563360': {
        'excess_min_pp': 5.0,
        'max_drawdown_max': -0.08,
        'trades_range': (10, 30),
        'wf_oos_excess_min': 0.0,
        'wf_decay_max': 0.60,
        'bull_low_pos_max': 0.30,
    },
}


def load_params(code, params_json=None):
    path = params_json or os.path.join(ROOT, 'runs', code, 'light_excess_results.json')
    if not os.path.exists(path):
        print(f"[{code}] 未找到 {path}")
        return None, path
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    top20 = data.get('top20', [])
    if not top20:
        print(f"[{code}] {path} 的 top20 为空")
        return None, path
    return top20[0]['params'], path


def run_backtest(code, params):
    """完整回测：返回 (results dict, backtest 对象)"""
    import config
    config.activate_profile(code)
    config.STRATEGY_MODE = 'V7'
    from param_optimizer import _build_regime_from_params, _reload_config_capture_modules
    from data_updater import load_data_from_db

    for k, v in params.items():
        if hasattr(config, k):
            setattr(config, k, v)
    if 'BULL_BUY_MULT' in params:
        config.REGIME_WEIGHTS = _build_regime_from_params(params)

    df = load_data_from_db()
    if df is None:
        return None, None
    start_date = str(df.index[0].date())

    strategy_mod, backtest_mod = _reload_config_capture_modules()
    strategy = strategy_mod.V6Strategy()
    signals = strategy.run(df)
    bt = backtest_mod.V6Backtest(df, start_date=start_date)
    results = bt.run(signals)
    return results, bt


def bull_low_pos_ratio(bt):
    """Bull 主升段低仓(<30%)占比：从 daily_signals 取 regime/current_position"""
    if bt is None or not getattr(bt, 'daily_signals', None):
        return None
    bull_days = [s for s in bt.daily_signals if s.get('regime') == 'Bull']
    if not bull_days:
        return None
    low = sum(1 for s in bull_days if s.get('current_position', 1.0) < 0.30)
    return low / len(bull_days)


def run_wf(code, params):
    """Walk-Forward OOS 超额 + IS→OOS 衰减"""
    import config
    config.activate_profile(code)
    config.STRATEGY_MODE = 'V7'
    from param_optimizer import run_walk_forward_validation, benchmark_beating_objective
    from data_updater import load_data_from_db

    df = load_data_from_db()
    if df is None:
        return None
    wf = run_walk_forward_validation(df, params, n_splits=3,
                                     objective_fn=benchmark_beating_objective)
    if wf is None or not wf.val_results:
        return None
    oos_excess = sum(r.excess_return for r in wf.val_results) / len(wf.val_results)
    is_excess = (sum(r.excess_return for r in wf.train_results) / len(wf.train_results)
                 if wf.train_results else 0.0)
    decay = (is_excess - oos_excess) / is_excess if is_excess > 1e-9 else None
    return {
        'wf_oos_excess': oos_excess,
        'wf_is_excess': is_excess,
        'wf_decay': decay,
        'wf_robustness': wf.robustness,
        'n_val_segments': len(wf.val_results),
    }


def check_line(name, success_line, actual, pass_cond):
    return {
        'name': name,
        'success_line': success_line,
        'actual': actual,
        'pass': bool(pass_cond),
    }


def accept(code, params_json=None):
    lines = SUCCESS_LINES.get(code)
    if lines is None:
        print(f"[{code}] 无成功线定义")
        return None

    params, src = load_params(code, params_json)
    if params is None:
        return None

    results, bt = run_backtest(code, params)
    if results is None:
        print(f"[{code}] 回测失败")
        return None

    excess_pp = (results.get('strategy_return', 0) - results.get('benchmark_return', 0)) * 100
    max_dd = results.get('max_drawdown', 0)
    bench_dd = results.get('benchmark_max_drawdown', 0)
    trades = results.get('total_trades', 0)
    bull_low = bull_low_pos_ratio(bt)
    wf = run_wf(code, params)

    metrics = {
        'strategy_return_pct': round(results.get('strategy_return', 0) * 100, 2),
        'benchmark_return_pct': round(results.get('benchmark_return', 0) * 100, 2),
        'excess_pp': round(excess_pp, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'benchmark_max_drawdown_pct': round(bench_dd * 100, 2),
        'total_trades': trades,
        'bull_low_pos_pct': round(bull_low * 100, 2) if bull_low is not None else None,
    }
    if wf:
        metrics.update({
            'wf_oos_excess_pp': round(wf['wf_oos_excess'] * 100, 2),
            'wf_is_excess_pp': round(wf['wf_is_excess'] * 100, 2),
            'wf_decay_pct': round(wf['wf_decay'] * 100, 2) if wf['wf_decay'] is not None else None,
            'wf_robustness': round(wf['wf_robustness'], 3),
            'wf_n_val_segments': wf['n_val_segments'],
        })

    checks = [
        check_line('全段超额', f"≥+{lines['excess_min_pp']:.0f}pp",
                   f"{excess_pp:+.2f}pp", excess_pp >= lines['excess_min_pp']),
        check_line('最大回撤', f"≤{lines['max_drawdown_max']*100:.0f}%",
                   f"{max_dd*100:.2f}%", max_dd >= lines['max_drawdown_max']),
        check_line('交易次数', f"{lines['trades_range'][0]}~{lines['trades_range'][1]}",
                   f"{trades}", lines['trades_range'][0] <= trades <= lines['trades_range'][1]),
    ]
    if wf:
        checks.append(check_line('WF OOS 超额', f">0pp",
                                 f"{wf['wf_oos_excess']*100:+.2f}pp",
                                 wf['wf_oos_excess'] > lines['wf_oos_excess_min']))
        decay_ok = (wf['wf_decay'] is not None and wf['wf_decay'] <= lines['wf_decay_max'])
        checks.append(check_line('IS→OOS 衰减', f"≤{lines['wf_decay_max']*100:.0f}%",
                                 f"{wf['wf_decay']*100:.2f}%" if wf['wf_decay'] is not None else 'N/A',
                                 decay_ok))
    if bull_low is not None:
        checks.append(check_line('Bull 低仓占比', f"<{lines['bull_low_pos_max']*100:.0f}%",
                                 f"{bull_low*100:.2f}%", bull_low < lines['bull_low_pos_max']))

    accepted = all(c['pass'] for c in checks)

    report = {
        'meta': {
            'code': code,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'params_source': src,
        },
        'params': params,
        'metrics': metrics,
        'checks': checks,
        'accepted': accepted,
    }

    out_path = os.path.join(ROOT, 'runs', code, 'acceptance_report.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=_json_default)

    print(f"\n[{code}] ========== P6.8 验收矩阵（成功线 §4.2）==========")
    print(f"  参数来源: {src}")
    print(f"  策略收益 {metrics['strategy_return_pct']}% | 基准 {metrics['benchmark_return_pct']}% | "
          f"超额 {metrics['excess_pp']:+.2f}pp")
    print(f"  回撤 {metrics['max_drawdown_pct']}% | 基准回撤 {metrics['benchmark_max_drawdown_pct']}% | "
          f"交易 {metrics['total_trades']}")
    for c in checks:
        mark = '✅' if c['pass'] else '❌'
        print(f"  {mark} {c['name']:<12} 成功线 {c['success_line']:<12} 实际 {c['actual']}")
    print(f"  >>> 验收{'通过' if accepted else '未通过'}（{sum(1 for c in checks if c['pass'])}/{len(checks)} 项达标）")
    print(f"[{code}] 报告已导出: {out_path}")
    return report


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    params_json = None
    if '--params-json' in sys.argv:
        params_json = sys.argv[sys.argv.index('--params-json') + 1]
    codes = args or ['589800', '563360']
    for code in codes:
        accept(code, params_json=params_json)
