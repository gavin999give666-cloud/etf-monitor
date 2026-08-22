"""
P6.7 敏感度筛查：light-excess 最优参数组 ±10% 扰动
=====================================================
设计文档 §3.3 纪律 3：Top-20 参数做敏感度扰动（±10%），净值变化 > 30% 视为悬崖参数，弃用。

- 对最优参数组内每个参数做 ±10% 双向扰动，重跑回测，测量净值变化
- 净值变化 > 30% → 悬崖参数（写回 profile 前必须处理）
- 输出: runs/{code}/sensitivity_report.json

用法:
  python tools/p6_7_sensitivity.py              # 两标的，top1
  python tools/p6_7_sensitivity.py 589800       # 单标的
  python tools/p6_7_sensitivity.py 589800 --top 3
"""
import json
import os
import sys
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'quant'))

CLIFF_THRESHOLD = 0.30  # 净值变化 >30% → 悬崖参数


def _json_default(o):
    """numpy 标量 → Python 原生类型（TrialResult 字段可能为 numpy.float64）"""
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    raise TypeError(f'Object of type {type(o).__name__} is not JSON serializable')


def load_best_params(code, top_n=1):
    path = os.path.join(ROOT, 'runs', code, 'light_excess_results.json')
    if not os.path.exists(path):
        print(f"[{code}] 未找到 {path}")
        return None
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    top20 = data.get('top20', [])
    if not top20:
        print(f"[{code}] top20 为空")
        return None
    return top20[:top_n]


def perturb_value(param, base, space):
    """对参数做 ±10% 扰动：int 四舍五入，超范围 clamp 到边界"""
    spec = space.get(param, {})
    ptype = spec.get('type', 'float')
    lo, hi = spec.get('range', (None, None))
    out = {}
    for label, mult in (('plus', 1.10), ('minus', 0.90)):
        v = base * mult
        if ptype == 'int':
            v = int(round(v))
        if lo is not None and hi is not None:
            v = max(lo, min(hi, v))
        out[label] = v
    return out


def eval_params(code, params):
    """用优化器同款评估路径重跑回测，返回净值代理（equity = 1 + strategy_return）"""
    import config
    config.activate_profile(code)
    config.STRATEGY_MODE = 'V7'
    from param_optimizer import _eval_single_plain_on_df
    from data_updater import load_data_from_db
    df = load_data_from_db()
    if df is None:
        return None
    start_date = str(df.index[0].date())
    result = _eval_single_plain_on_df(params, df, start_date)
    if result is None:
        return None
    return {
        'equity': 1.0 + result.strategy_return,
        'strategy_return': result.strategy_return,
        'excess_return': result.excess_return,
        'max_drawdown': result.max_drawdown,
        'total_trades': result.total_trades,
    }


def screen(code, top_n=1):
    import param_optimizer as po

    candidates = load_best_params(code, top_n)
    if not candidates:
        return None
    best = candidates[0]
    params = best['params']
    space = po.EXCESS_SEARCH_SPACE

    print(f"\n[{code}] ========== P6.7 敏感度筛查（top{top_n}，±10% 扰动）==========")
    base = eval_params(code, params)
    if base is None:
        print(f"[{code}] 基准参数评估失败")
        return None
    print(f"  基准: 收益={base['strategy_return']*100:.2f}% | 净值={base['equity']:.4f} | "
          f"回撤={base['max_drawdown']*100:.2f}% | 交易={base['total_trades']}")

    report = {
        'meta': {
            'code': code,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'light_excess_results.json',
            'cliff_threshold': CLIFF_THRESHOLD,
            'top_n': top_n,
        },
        'base': base,
        'cliff_params': [],
        'params': {},
    }

    print(f"\n  {'参数':<32}{'base':>10}{'+10%净值':>10}{'-10%净值':>10}{'最大变化':>9}  悬崖")
    print('  ' + '-' * 90)

    for param in sorted(params.keys()):
        base_val = params[param]
        pert = perturb_value(param, base_val, space)
        row = {'base': base_val, 'plus': None, 'minus': None}
        max_change = 0.0
        for label, pval in pert.items():
            p_params = dict(params)
            p_params[param] = pval
            res = eval_params(code, p_params)
            if res is None:
                continue
            change = abs(res['equity'] - base['equity']) / base['equity']
            max_change = max(max_change, change)
            row[label] = {
                'value': pval,
                'equity': res['equity'],
                'strategy_return': res['strategy_return'],
                'excess_return': res['excess_return'],
                'max_drawdown': res['max_drawdown'],
                'total_trades': res['total_trades'],
                'equity_change_pct': round(change * 100, 2),
            }
        is_cliff = max_change > CLIFF_THRESHOLD
        row['max_change_pct'] = round(max_change * 100, 2)
        row['is_cliff'] = is_cliff
        report['params'][param] = row
        if is_cliff:
            report['cliff_params'].append(param)

        p_eq = row['plus']['equity'] if row['plus'] else float('nan')
        m_eq = row['minus']['equity'] if row['minus'] else float('nan')
        flag = '⚠️ 悬崖' if is_cliff else ''
        print(f"  {param:<32}{base_val:>10.4f}{p_eq:>10.4f}{m_eq:>10.4f}"
              f"{row['max_change_pct']:>8.2f}%  {flag}")

    out_path = os.path.join(ROOT, 'runs', code, 'sensitivity_report.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=_json_default)
    print(f"\n[{code}] 悬崖参数 ({len(report['cliff_params'])}): "
          f"{report['cliff_params'] if report['cliff_params'] else '无'}")
    print(f"[{code}] 报告已导出: {out_path}")
    return report


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    top_n = 1
    if '--top' in sys.argv:
        top_n = int(sys.argv[sys.argv.index('--top') + 1])
    codes = args or ['589800', '563360']
    for code in codes:
        screen(code, top_n=top_n)
