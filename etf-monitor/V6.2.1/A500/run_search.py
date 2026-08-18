"""
参数穷举搜索
单进程串行，数据+指标加载一次，每组 purge+重导入
中断后重新运行自动断点续跑

用法：在你自己的终端运行
    python run_search.py
"""

import sys, os, copy, csv, itertools, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

STAGE1_GRID = {
    'MIN_HOLD_DAYS':          [10, 15, 20, 25, 30],
    'SCORE_HOLD_ZONE':        [15, 20, 25],
    'BULL_SELL_DIV':          [0.12, 0.20, 0.30],
    'BULL_BUY_MULT':          [1.30, 1.50, 1.80],
    'TRADE_TARGET_DELTA':     [0.02, 0.05],
    'MAX_POSITION':           [0.85, 0.95],
    'CONFIRMATION_THRESHOLD': [65, 75],
}
# 5x3x3x3x2x2x2 = 1,080 combos, ~80 min

RESULT_COLS = ['strategy_return','sharpe_ratio','max_drawdown','calmar_ratio',
               'win_rate','total_trades','avg_hold_days','excess_return',
               'profit_factor','expectancy']

RELOAD_MODULES = [
    'strategy','backtest','behavior_detector','regime_detector',
    'emotion_builder','event_engine','reward_risk','evidence_engine',
    'position_manager','scoring_engine','crowd_psychology','indicators',
    'replay_engine','param_search','behavior_memory',
]

PARAM_KEYS = list(STAGE1_GRID.keys())
CSV_PATH = 'stage1.csv'


def gen_combos(grid):
    ks, vs = list(grid.keys()), list(grid.values())
    return [dict(zip(ks, c)) for c in itertools.product(*vs)]


def load_done():
    if not os.path.exists(CSV_PATH): return set(), []
    rows, keys = [], set()
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append(r)
            keys.add(tuple(sorted((k, r[k]) for k in PARAM_KEYS if k in r)))
    return keys, rows


def save_csv(rows):
    with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=PARAM_KEYS + RESULT_COLS, extrasaction='ignore')
        w.writeheader()
        for r in rows: w.writerow(r)


def purge():
    for m in list(sys.modules.keys()):
        if m in RELOAD_MODULES: del sys.modules[m]


def run_one(params, df, orig_regime):
    import config as cfg
    cfg.MIN_HOLD_DAYS = params['MIN_HOLD_DAYS']
    cfg.SCORE_HOLD_ZONE = params['SCORE_HOLD_ZONE']
    cfg.TRADE_TARGET_DELTA = params['TRADE_TARGET_DELTA']
    cfg.MAX_POSITION = params['MAX_POSITION']
    cfg.CONFIRMATION_THRESHOLD = params['CONFIRMATION_THRESHOLD']
    cfg.REGIME_WEIGHTS = copy.deepcopy(orig_regime)
    cfg.REGIME_WEIGHTS['Bull']['sell_div'] = params['BULL_SELL_DIV']
    cfg.REGIME_WEIGHTS['Bull']['buy_mult'] = params['BULL_BUY_MULT']

    purge()
    from strategy import V6Strategy
    from backtest import V6Backtest

    s = V6Strategy()
    sig = s.run(df)
    bt = V6Backtest(df, strategy=s)
    bt.run(sig)
    r = bt._compute_results()

    row = {k: params[k] for k in PARAM_KEYS}
    row['strategy_return'] = round(r.get('strategy_return', 0), 6)
    row['sharpe_ratio']   = round(r.get('sharpe_ratio', 0), 6)
    row['max_drawdown']   = round(r.get('max_drawdown', 0), 6)
    row['calmar_ratio']   = round(r.get('calmar_ratio', 0), 6)
    row['win_rate']       = round(r.get('win_rate', 0), 6)
    row['total_trades']   = int(r.get('total_trades', 0))
    row['avg_hold_days']  = round(r.get('avg_hold_days', 0), 6)
    row['excess_return']  = round(r.get('excess_return', 0), 6)
    row['profit_factor']  = round(r.get('profit_factor', 0), 6)
    row['expectancy']     = round(r.get('expectancy', 0), 6)
    return row


def analyze(csv_rows):
    valid = [r for r in csv_rows if r.get('strategy_return')]
    if not valid: return
    by_ret = sorted(valid, key=lambda x: x['strategy_return'], reverse=True)
    print(f"\n{'='*60}")
    print(f"  Top 10 by Return")
    print(f"{'='*60}")
    for i, r in enumerate(by_ret[:10]):
        ps = ' '.join([f"{k}={r[k]}" for k in PARAM_KEYS])
        print(f"  {i+1}. {r['strategy_return']*100:+.2f}%  "
              f"Sharpe:{r['sharpe_ratio']:.3f}  DD:{r['max_drawdown']*100:.1f}%  "
              f"Trades:{r['total_trades']}  Hold:{r['avg_hold_days']:.0f}d  |  {ps}")
    best = by_ret[0]
    print(f"\n  >>> BEST: {best['strategy_return']*100:.2f}%  "
          f"Sharpe:{best['sharpe_ratio']:.3f}  Win:{best['win_rate']*100:.0f}%  "
          f"Trades:{best['total_trades']}")
    print(f"      MIN_HOLD_DAYS={best['MIN_HOLD_DAYS']}  "
          f"SCORE_HOLD_ZONE={best['SCORE_HOLD_ZONE']}  "
          f"BULL_SELL_DIV={best['BULL_SELL_DIV']}  "
          f"BULL_BUY_MULT={best['BULL_BUY_MULT']}")


def main():
    print(f"Grid Search  |  {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)

    all_combos = gen_combos(STAGE1_GRID)
    total = len(all_combos)

    done_keys, csv_rows = load_done()
    remaining = [c for c in all_combos if tuple(sorted(c.items())) not in done_keys]

    print(f"Total: {total}  |  Done: {len(csv_rows)}  |  Remaining: {len(remaining)}", flush=True)
    if not remaining:
        print("All done!", flush=True)
        analyze(csv_rows)
        return

    import config as cfg
    orig_regime = copy.deepcopy(cfg.REGIME_WEIGHTS)
    from data_updater import load_data_from_db
    from indicators import calculate_indicators
    df = load_data_from_db()
    df = calculate_indicators(df)
    print(f"Data: {len(df)} rows, {df.index[0].date()} ~ {df.index[-1].date()}", flush=True)

    t0 = time.time()
    errors = save_at = 0

    for i, params in enumerate(remaining):
        try:
            row = run_one(params, df, orig_regime)
            csv_rows.append(row)
        except Exception:
            errors += 1
            csv_rows.append({k: params[k] for k in PARAM_KEYS})

        done = len(done_keys) + i + 1
        if done % 20 == 0 or done == total:
            elapsed = time.time() - t0
            rate = (i+1)/elapsed if elapsed>0 else 0
            eta = (len(remaining)-i-1)/rate if rate>0 else 0
            ret = f"{row.get('strategy_return',0)*100:+6.2f}%" if 'strategy_return' in row else "ERR"
            print(f"  [{done:>4}/{total} {done/total*100:5.1f}%]  "
                  f"{elapsed/60:.0f}m  {rate*60:.0f}/m  ETA:{eta/60:.0f}m  {ret}", flush=True)
        if done - save_at >= 100:
            save_csv(csv_rows); save_at = done

    save_csv(csv_rows)
    print(f"\nDone! {len(csv_rows)} rows, {errors} err, {(time.time()-t0)/60:.0f} min", flush=True)
    analyze(csv_rows)


if __name__ == '__main__':
    main()
