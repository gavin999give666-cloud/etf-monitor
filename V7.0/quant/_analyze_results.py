"""分析网格搜索结果"""
import csv, os, sys, statistics

os.chdir(os.path.dirname(os.path.abspath(__file__)))

rows = []
with open('stage1.csv', 'r', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        rows.append(r)

valid = [r for r in rows if r.get('strategy_return')]
print(f'Total rows: {len(rows)}')
print(f'Valid (with returns): {len(valid)}')

by_ret = sorted(valid, key=lambda x: float(x['strategy_return']), reverse=True)

print()
print('=' * 90)
print('  TOP 10 by Return')
print('=' * 90)
for i, r in enumerate(by_ret[:10]):
    ret = float(r['strategy_return'])
    sharpe = float(r['sharpe_ratio'])
    dd = float(r['max_drawdown'])
    calmar = float(r['calmar_ratio'])
    win = float(r['win_rate'])
    trades = int(r['total_trades'])
    hold = float(r['avg_hold_days'])
    excess = float(r['excess_return'])
    pf = float(r['profit_factor'])
    expc = float(r['expectancy'])
    print(f'{i+1}. Return:{ret*100:+.2f}%  Excess:{excess*100:+.2f}%  '
          f'Sharpe:{sharpe:.3f}  DD:{dd*100:.1f}%  Calmar:{calmar:.3f}  '
          f'Win:{win*100:.0f}%  Trades:{trades}  Hold:{hold:.0f}d  '
          f'PF:{pf:.2f}  Exp:{expc*100:+.2f}%')
    print(f'     MIN_HOLD={r["MIN_HOLD_DAYS"]}  SCORE_HOLD={r["SCORE_HOLD_ZONE"]}  '
          f'BULL_SELL_DIV={r["BULL_SELL_DIV"]}  BULL_BUY_MULT={r["BULL_BUY_MULT"]}  '
          f'DELTA={r["TRADE_TARGET_DELTA"]}  MAX_POS={r["MAX_POSITION"]}  '
          f'CONFIRM={r["CONFIRMATION_THRESHOLD"]}')

# Show bottom 5
print()
print('=' * 90)
print('  BOTTOM 5 (Worst)')
print('=' * 90)
for i, r in enumerate(by_ret[-5:]):
    ret = float(r['strategy_return'])
    dd = float(r['max_drawdown'])
    print(f'{i+1}. Return:{ret*100:+.2f}%  DD:{dd*100:.1f}%  '
          f'MIN_HOLD={r["MIN_HOLD_DAYS"]}  BULL_BUY_MULT={r["BULL_BUY_MULT"]}  '
          f'DELTA={r["TRADE_TARGET_DELTA"]}')

# Summary stats
returns = [float(r['strategy_return']) for r in valid]
sharpes = [float(r['sharpe_ratio']) for r in valid]
dd = [float(r['max_drawdown']) for r in valid]

print()
print('=' * 90)
print(f'  SUMMARY ({len(valid)} combos)')
print('=' * 90)
print(f'  Return: min={min(returns)*100:.2f}%  max={max(returns)*100:.2f}%  '
      f'mean={statistics.mean(returns)*100:.2f}%  median={statistics.median(returns)*100:.2f}%')
print(f'  Sharpe: min={min(sharpes):.3f}  max={max(sharpes):.3f}  '
      f'mean={statistics.mean(sharpes):.3f}  median={statistics.median(sharpes):.3f}')
print(f'  MaxDD:  min={min(dd)*100:.1f}%  max={max(dd)*100:.1f}%  '
      f'mean={statistics.mean(dd)*100:.1f}%  median={statistics.median(dd)*100:.1f}%')

# Parameter analysis: which params matter most?
print()
print('=' * 90)
print('  PARAMETER IMPACT (group means by param value)')
print('=' * 90)
param_keys = ['MIN_HOLD_DAYS', 'SCORE_HOLD_ZONE', 'BULL_SELL_DIV', 'BULL_BUY_MULT',
              'TRADE_TARGET_DELTA', 'MAX_POSITION', 'CONFIRMATION_THRESHOLD']
for pk in param_keys:
    groups = {}
    for r in valid:
        val = r[pk]
        if val not in groups:
            groups[val] = []
        groups[val].append(float(r['strategy_return']))
    print(f'\n  [{pk}]:')
    for val in sorted(groups.keys(), key=lambda x: float(x)):
        mean_ret = statistics.mean(groups[val])
        count = len(groups[val])
        bar = '+' if mean_ret > 0 else '-'
        print(f'    {pk}={val}:  mean={mean_ret*100:+.2f}%  n={count}')
