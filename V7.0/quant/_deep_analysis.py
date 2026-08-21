"""
深度诊断脚本：
1. 读取数据库收盘价，分析实际趋势
2. 读取每笔回测交易，逐笔诊断对错
3. 计算哪些参数需要调整，什么方向
"""
import sqlite3
import pandas as pd
import numpy as np
import json
import os

db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_data.db')

# ============== 1. 读取历史数据 ==============
conn = sqlite3.connect(db_path)
df = pd.read_sql("SELECT * FROM stock_data ORDER BY date", conn, parse_dates=['date'])
conn.close()
df.set_index('date', inplace=True)
df = df.sort_index()
close = df['close']

print("=" * 80)
print("V6.2.3 深度诊断：策略交易 vs 价格实际走势")
print("=" * 80)

# 基准数据
start_price = close.iloc[0]
end_price = close.iloc[-1]
benchmark = (end_price - start_price) / start_price * 100
print(f"\n数据范围: {close.index[0].strftime('%Y-%m-%d')} ~ {close.index[-1].strftime('%Y-%m-%d')}")
print(f"首日价: {start_price:.4f} | 最新价: {end_price:.4f}")
print(f"基准收益: {benchmark:+.2f}%")

# ============== 2. 读取交易记录 ==============
with open('backtest_records.json', 'r', encoding='utf-8') as f:
    records = json.load(f)

trades = records['trades']
perf = records['performance']

print(f"\n策略收益: {perf['strategy_return_pct']:+.2f}%")
print(f"超额收益: {perf['excess_return_pct']:+.2f}%")
print(f"总交易: {perf['total_trades']} | 盈利: {perf['winning_trades']} | 亏损: {perf['losing_trades']} | 胜率: {perf['win_rate_pct']}%")
print(f"平均持仓: {perf['avg_hold_days']}天 | 最大回撤: {perf['max_drawdown_pct']}%")
print(f"夏普: {perf['sharpe_ratio']} | 盈亏比: {perf['profit_factor']}")

# ============== 3. 逐笔交易深度诊断 ==============
print("\n" + "=" * 100)
print("逐笔交易深度诊断")
print("=" * 100)

# 用更丰富的方式计算每笔交易的"买后走势"
for t in trades:
    tid = t.get('trade_id', '?')
    entry_date_str = t['entry_date'][:10] if t['entry_date'] else ''
    exit_date_str = t['exit_date'][:10] if t['exit_date'] else ''
    pnl = t['pnl_pct']
    label = t['pnl_label']
    entry_behavior = ', '.join(t.get('entry_behavior', []))
    exit_behavior = ', '.join(t.get('exit_behavior', []))
    regime = t.get('entry_regime', '?')
    psych = t.get('entry_psychology', '?')
    entry_score = t.get('entry_score', 0)
    exit_score = t.get('exit_score', 0)

    try:
        entry_dt = pd.Timestamp(entry_date_str)
        exit_dt = pd.Timestamp(exit_date_str)

        # 买入后的市场走势
        entry_price_val = close.get(entry_dt, None)

        # 卖出后至今的走势
        if exit_dt in close.index:
            exit_idx = close.index.get_loc(exit_dt)
            sell_price_val = close.iloc[exit_idx]
            future_close = close.iloc[exit_idx+1:] if exit_idx + 1 < len(close) else pd.Series()
            if len(future_close) > 0:
                future_high = future_close.max()
                future_low = future_close.min()
                future_end = future_close.iloc[-1]
                future_upside = (future_high - sell_price_val) / sell_price_val * 100
                future_downside = (future_low - sell_price_val) / sell_price_val * 100
                future_to_end = (future_end - sell_price_val) / sell_price_val * 100
            else:
                future_upside = future_downside = future_to_end = 0
        else:
            future_upside = future_downside = future_to_end = 0

        # 计算买卖期间大盘的实际涨幅（如果买入持有）
        if entry_dt in close.index and exit_dt in close.index:
            entry_idx = close.index.get_loc(entry_dt)
            exit_idx = close.index.get_loc(exit_dt)
            segment = close.iloc[entry_idx:exit_idx+1]
            segment_return = (segment.iloc[-1] - segment.iloc[0]) / segment.iloc[0] * 100
            segment_high = segment.max()
            segment_low = segment.min()
            max_possible = (segment.max() - segment.iloc[0]) / segment.iloc[0] * 100
            max_drawdown = (segment.min() - segment.iloc[0]) / segment.iloc[0] * 100
        else:
            segment_return = 0
            max_possible = 0
            max_drawdown = 0

        # 判断这笔交易的质量
        verdict = ""
        if label == 'WIN' and future_to_end > 2:
            verdict = "盈利但卖出过早！卖后继续涨{:.1f}%".format(future_to_end)
        elif label == 'WIN' and future_to_end < -2:
            verdict = "盈利且卖出精准！卖后下跌{:.1f}%".format(abs(future_to_end))
        elif label == 'WIN':
            verdict = "盈利 (卖后小幅波动)"
        elif label == 'LOSS' and future_to_end > 5:
            verdict = "亏损且卖出过早！卖后涨{:.1f}%（应持有）".format(future_to_end)
        elif label == 'LOSS' and future_to_end < -5:
            verdict = "亏损但卖出正确！卖后继续跌{:.1f}%".format(abs(future_to_end))
        else:
            verdict = f"亏损 (卖后变动{future_to_end:+.1f}%)"

    except Exception as e:
        verdict = f"分析异常: {e}"
        future_upside = future_downside = future_to_end = 0
        segment_return = max_possible = max_drawdown = 0

    print(f"\n--- 交易 #{tid} [{label}] ------------------------------")
    print(f"  日期: {entry_date_str} → {exit_date_str}")
    print(f"  价格: {t['entry_price']:.4f} → {t['exit_price']:.4f}  | 收益: {pnl:+.2f}%")
    print(f"  买入: {entry_behavior} | Regime={regime} | Psych={psych} | Score={entry_score}")
    print(f"  卖出: {exit_behavior} | Score={exit_score}")
    print(f"  期间大盘: {segment_return:+.2f}% | 最高可达: {max_possible:+.2f}% | 最差: {max_drawdown:+.2f}%")
    print(f"  卖后至今: 最高{'+' if future_upside>=0 else ''}{future_upside:.1f}% / 最低{future_downside:+.1f}% / 终{future_to_end:+.1f}%")
    print(f"  >> {verdict}")

# ============== 4. 分类统计 ==============
print("\n" + "=" * 80)
print("分类统计：买入行为 x 市场状态 交叉分析")
print("=" * 80)

from collections import defaultdict
cross_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'total_pnl': 0, 'pnls': []})

for t in trades:
    for b in t.get('entry_behavior', []):
        regime = t.get('entry_regime', 'Unknown')
        key = f"{b} @ {regime}"
        cross_stats[key]['count'] += 1
        cross_stats[key]['total_pnl'] += t['pnl_pct']
        cross_stats[key]['pnls'].append(t['pnl_pct'])
        if t['pnl_label'] == 'WIN':
            cross_stats[key]['wins'] += 1

print(f"\n{'行为 @ 市场状态':<35} {'次数':>5} {'胜率':>7} {'平均盈亏':>9} {'总盈亏':>9}")
print("-" * 70)
for key in sorted(cross_stats.keys()):
    s = cross_stats[key]
    wr = s['wins'] / s['count'] * 100 if s['count'] > 0 else 0
    avg = s['total_pnl'] / s['count'] if s['count'] > 0 else 0
    print(f"  {key:<33} {s['count']:>5} {wr:>6.1f}% {avg:>+8.2f}% {s['total_pnl']:>+8.2f}%")

# ============== 5. 心理状态分析 ==============
print("\n" + "=" * 80)
print("买入心理状态 vs 盈亏分析")
print("=" * 80)

psych_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'total_pnl': 0})
for t in trades:
    psych = t.get('entry_psychology', 'Unknown')
    psych_stats[psych]['count'] += 1
    psych_stats[psych]['total_pnl'] += t['pnl_pct']
    if t['pnl_label'] == 'WIN':
        psych_stats[psych]['wins'] += 1

print(f"\n{'心理状态':<20} {'交易次数':>8} {'胜率':>7} {'平均盈亏':>9} {'总盈亏':>9}")
print("-" * 60)
for psych in ['Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion']:
    s = psych_stats.get(psych, {'count': 0, 'wins': 0, 'total_pnl': 0})
    if s['count'] > 0:
        wr = s['wins'] / s['count'] * 100
        avg = s['total_pnl'] / s['count']
        print(f"  {psych:<20} {s['count']:>8} {wr:>6.1f}% {avg:>+8.2f}% {s['total_pnl']:>+8.2f}%")
    else:
        print(f"  {psych:<20} {'0':>8}")

# ============== 6. 买卖时机质量评分 ==============
print("\n" + "=" * 80)
print("买卖时机质量分析：对照'理论最佳买卖点'")
print("=" * 80)

# 理论最佳买入日 (来自之前分析)
best_buy_dates = [
    ('2025-04-07', 0.9050, '恐慌低点'),
    ('2025-04-08', 0.9270, '继续探底'),
    ('2025-01-10', 0.9580, '年初回调'),
    ('2026-03-23', 1.2250, '回调低点'),
    ('2025-11-21', 1.1880, '盘整低点'),
    ('2025-06-20', 0.9930, '起涨前低点'),
]

best_sell_dates = [
    ('2025-08-25', 1.1840, '中期高点'),
    ('2025-08-22', 1.1590, '前一个高点'),
    ('2025-09-11', 1.2180, '反弹高点'),
    ('2026-05-11', 1.3990, '年度高点附近'),
    ('2026-06-22', 1.4320, '历史最高点'),
    ('2025-07-29', 1.0940, '第一阶段高点'),
]

print("\n理论最佳买入日 vs 策略实际买入:")
for bd, bp, desc in best_buy_dates:
    # 找最近的实际买入
    bd_dt = pd.Timestamp(bd)
    actual_nearby = []
    for t in trades:
        ed = pd.Timestamp(t['entry_date'][:10])
        if abs((ed - bd_dt).days) <= 10:
            actual_nearby.append((t['entry_date'][:10], t['pnl_pct'], t.get('entry_behavior', [])))
    if actual_nearby:
        for a in actual_nearby:
            print(f"  {bd}({desc}): 附近买入 {a[0]} PnL={a[1]:+.2f}% 行为={a[2]}")
    else:
        print(f"  {bd}({desc}): 【未捕捉！】策略错过了这个买入机会")

print("\n理论最佳卖出日 vs 策略实际卖出:")
for sd, sp, desc in best_sell_dates:
    sd_dt = pd.Timestamp(sd)
    actual_nearby = []
    for t in trades:
        xd = pd.Timestamp(t['exit_date'][:10])
        if abs((xd - sd_dt).days) <= 10:
            actual_nearby.append((t['exit_date'][:10], t['pnl_pct'], t.get('exit_behavior', [])))
    if actual_nearby:
        for a in actual_nearby:
            print(f"  {sd}({desc}): 附近卖出 {a[0]} PnL={a[1]:+.2f}% 行为={a[2]}")
    else:
        print(f"  {sd}({desc}): 【未捕捉！】策略错过了这个卖出机会")

# ============== 7. 关键结论 ==============
print("\n" + "=" * 80)
print("关键诊断结论")
print("=" * 80)

# 计算卖出后错过的涨幅
total_missed = 0
early_sell_count = 0
for t in trades:
    try:
        exit_dt = pd.Timestamp(t['exit_date'][:10])
        if exit_dt in close.index:
            exit_idx = close.index.get_loc(exit_dt)
            if exit_idx + 1 < len(close):
                future = close.iloc[exit_idx+1:]
                sell_price = close.iloc[exit_idx]
                # 卖出后N天的涨幅
                for days in [5, 10, 20]:
                    pass  # skip for now
                future_to_end = (future.iloc[-1] - sell_price) / sell_price * 100
                if future_to_end > 3:
                    total_missed += future_to_end
                    early_sell_count += 1
    except:
        pass

# 计算策略在牛市中表现
bull_trades = [t for t in trades if t.get('entry_regime') == 'Bull']
bear_trades = [t for t in trades if t.get('entry_regime') == 'Bear']
range_trades = [t for t in trades if t.get('entry_regime') == 'Range']

print(f"\n牛市买入: {len(bull_trades)}笔 | 熊市买入: {len(bear_trades)}笔 | 震荡买入: {len(range_trades)}笔")
if bull_trades:
    bull_avg = np.mean([t['pnl_pct'] for t in bull_trades])
    bull_wr = sum(1 for t in bull_trades if t['pnl_label'] == 'WIN') / len(bull_trades) * 100
    print(f"  牛市平均盈亏: {bull_avg:+.2f}% | 胜率: {bull_wr:.0f}%")

# 评分与收益相关性
buy_scores = [t.get('entry_score', 0) for t in trades]
pnls = [t['pnl_pct'] for t in trades]
if len(buy_scores) > 3:
    corr = np.corrcoef(buy_scores, pnls)[0, 1]
    print(f"\n买入评分 vs 实际收益 相关系数: {corr:.3f} ({'正相关' if corr > 0 else '负相关'})")

# 持仓天数 vs 收益
hold_days_list = []
for t in trades:
    try:
        ed = pd.Timestamp(t['entry_date'][:10])
        xd = pd.Timestamp(t['exit_date'][:10])
        hold_days_list.append((xd - ed).days)
    except:
        pass
if len(hold_days_list) > 3:
    corr2 = np.corrcoef(hold_days_list, pnls)[0, 1]
    print(f"持仓天数 vs 实际收益 相关系数: {corr2:.3f} ({'正相关-持仓越长越好' if corr2 > 0 else '负相关-持仓越长越差'})")

print(f"\n卖出过早笔数: {early_sell_count}/{len(trades)}")
print(f"卖出后平均继续涨幅: {total_missed/early_sell_count if early_sell_count else 0:.1f}%")

print("\n" + "=" * 80)
print("分析完成")
print("=" * 80)
