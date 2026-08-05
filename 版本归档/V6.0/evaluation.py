"""
V6.0 策略评估模块
==================

V6.0 新增输出：
- Evidence Engine 分解输出
- Replay Learning 行为成功率统计
- 情绪双确认统计
- 行为记忆库持久化

输出：
1. 回测绩效指标
2. 行为统计
3. 事件统计
4. 评分统计（含 Reward/Risk）
5. Behavior Memory 统计（V6 新增）
6. Evidence Engine 融合统计（V6 新增）
7. 情绪双确认统计（V6 新增）
8. Replay Engine 验证
9. 参数敏感性分析
10. 可视化
"""
import pandas as pd
import numpy as np
import math
from datetime import datetime

from strategy import V6Strategy
from backtest import V6Backtest
from replay_engine import ReplayEngine
from data_updater import load_data_from_db

try:
    import matplotlib
    matplotlib.use('TkAgg')
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter
    import matplotlib.dates as mdates
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


def evaluate_strategy(use_ml=False, emotion_method='weighted'):
    """V6.0 完整策略评估"""
    print("=" * 80)
    print("A500 ETF V6.0 Evidence Engine + Replay Learning 策略 回测评估")
    print("=" * 80)
    print(f"评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"配置: ML={'ON' if use_ml else 'OFF'}, Emotion={emotion_method}")

    df = load_data_from_db()
    if df is None:
        print("无法获取历史数据，评估终止")
        return

    print(f"\n数据范围: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"数据条数: {len(df)}")

    # Step 1: 生成信号
    print("\n[1/7] 运行 V6.0 七层策略流水线 (Evidence Engine)...")
    strategy = V6Strategy(use_ml=use_ml, emotion_method=emotion_method)
    signals = strategy.run(df)
    print(f"  信号条数: {len(signals)}")

    # Step 2: 运行回测（集成Replay Learning）
    print("[2/7] 运行增强回测引擎 (Replay Learning)...")
    bt = V6Backtest(df, start_date='2025-09-01', strategy=strategy)
    results = bt.run(signals)

    if not results:
        print("回测失败")
        return

    # Step 3: 输出绩效
    print_performance(results)

    # Step 4: 行为 + 事件统计
    behavior_stats = bt.get_behavior_statistics()
    event_stats = bt.get_event_statistics()
    print_behavior_statistics(behavior_stats, event_stats)

    # Step 5: 评分统计
    score_stats = bt.get_score_statistics()
    print_score_statistics(score_stats)

    # Step 6: V6 Behavior Memory 统计
    print_behavior_memory(strategy)

    # Step 7: V6 情绪双确认统计
    print_emotion_confirmation(bt, strategy)

    # Step 8: 行为贡献
    print_behavior_contribution(behavior_stats)

    # Step 9: Replay Engine 验证
    print("\n[Replay 引擎验证]")
    replay_engine = ReplayEngine(strategy.replay_records, bt.trades)
    valid, errors = replay_engine.validate()
    if valid:
        print("  所有交易均有完整解释 ✓")
    else:
        print(f"  警告: {len(errors)} 笔交易缺少完整解释！")
        for e in errors[:5]:
            print(f"    - {e['message']}")

    # 打印最近交易解释
    if bt.trades:
        print("\n[最近一笔交易解释]")
        replay_engine.print_trade_explanation()

    # V6: 打印促成因子 & 导出交易文件
    if bt.trades:
        print("\n[V6.0 交易促成因子]")
        bt.print_trade_factors(-1)       # 最后一笔
        if len(bt.trades) >= 2:
            bt.print_trade_factors(0)    # 第一笔（通常是盈利样本）
        bt.export_trades()               # 导出到 trades_v6.json

    # Step 10: 参数敏感性分析
    print("\n[参数敏感性分析]")
    run_sensitivity_analysis(df, use_ml, emotion_method)

    # Step 11: 绘图
    if HAS_MATPLOTLIB:
        print("\n[图表生成]")
        plot_results(df, signals, bt, results)

    # 打印情绪状态分布
    print_psychology_distribution(bt)

    print("\n" + "=" * 80)
    print("V6.0 策略评估完成！")
    print("=" * 80)


def print_performance(results):
    """打印绩效指标"""
    print("\n" + "=" * 80)
    print("回测绩效指标 (Performance Metrics)")
    print("=" * 80)
    print(f"\n  {'策略收益率:':<30} {results['strategy_return'] * 100:>8.2f}%")
    print(f"  {'基准收益率(买入持有):':<30} {results['benchmark_return'] * 100:>8.2f}%")
    print(f"  {'超额收益:':<30} {results['excess_return'] * 100:>+8.2f}%")
    print(f"  {'最大回撤:':<30} {results['max_drawdown'] * 100:>8.2f}%")
    print(f"  {'年化收益率:':<30} {results['annualized_return'] * 100:>8.2f}%")
    print(f"  {'年化波动率:':<30} {results['volatility'] * 100:>8.2f}%")
    print(f"  {'夏普比率:':<30} {results['sharpe_ratio']:>8.3f}")
    print(f"  {'Calmar Ratio:':<30} {results['calmar_ratio']:>8.3f}")
    print(f"  {'盈亏比:':<30} {results['profit_factor']:>8.2f}")
    print(f"  {'胜率:':<30} {results['win_rate'] * 100:>8.2f}%")
    print(f"  {'总交易次数:':<30} {results['total_trades']:>8}")
    print(f"  {'盈利交易:':<30} {results['winning_trades']:>8}")
    print(f"  {'亏损交易:':<30} {results['losing_trades']:>8}")
    print(f"  {'最终资产:':<30} {results['final_equity']:>10.2f}")


def print_behavior_statistics(behavior_stats, event_stats):
    """打印行为 + 事件统计"""
    print("\n" + "=" * 80)
    print("行为 + 事件统计")
    print("=" * 80)

    print(f"\n  {'行为名称':<25} {'出现次数':>8} {'触发交易':>8} {'平均贡献':>10} {'总贡献':>10}")
    print(f"  {'-'*65}")

    buy_names = ['DoubleBottom', 'PanicSell', 'TrendPullback', 'BreakoutConfirm']
    sell_names = ['MomentumExhaustion', 'TrendFailure', 'FalseBreak']

    print("  --- 买入行为 ---")
    for name in buy_names:
        s = behavior_stats.get(name, {})
        avg_c = s.get('avg_contribution', 0) * 100
        tot_c = s.get('total_contribution', 0) * 100
        print(f"  {name:<25} {s.get('count', 0):>8} {s.get('trades', 0):>8} {avg_c:>9.2f}% {tot_c:>9.2f}%")

    print("\n  --- 卖出行为 ---")
    for name in sell_names:
        s = behavior_stats.get(name, {})
        avg_c = s.get('avg_contribution', 0) * 100
        tot_c = s.get('total_contribution', 0) * 100
        print(f"  {name:<25} {s.get('count', 0):>8} {s.get('trades', 0):>8} {avg_c:>9.2f}% {tot_c:>9.2f}%")

    print(f"\n  --- 事件生命周期统计 ---")
    print(f"  总计确认买入事件: {event_stats.get('confirmed_buy', 0)}")
    print(f"  总计确认卖出事件: {event_stats.get('confirmed_sell', 0)}")
    print(f"  最大同时活跃事件: {event_stats.get('max_active_events', 0)}")


def print_score_statistics(stats):
    """打印评分统计"""
    print("\n" + "=" * 80)
    print("V6.0 评分统计 (含 Reward/Risk)")
    print("=" * 80)
    if not stats:
        print("  无评分数据")
        return

    print(f"\n  平均BuyScore:   {stats['avg_buy_score']:>8.1f}")
    print(f"  平均SellScore:  {stats['avg_sell_score']:>8.1f}")
    print(f"  最大BuyScore:   {stats['max_buy_score']:>8.1f}")
    print(f"  最大SellScore:  {stats['max_sell_score']:>8.1f}")
    print(f"  平均Reward:     {stats.get('avg_reward', 0):>8.1f}")
    print(f"  平均Risk:       {stats.get('avg_risk', 0):>8.1f}")


def print_behavior_memory(strategy):
    """V6.0: 打印行为记忆库统计"""
    print("\n" + "=" * 80)
    print("V6.0 Behavior Memory (Replay Learning) 统计")
    print("=" * 80)

    bm = strategy.get_behavior_memory()
    if bm:
        bm.print_stats()
    else:
        print("  行为记忆库未启用或为空（Replay Learning = OFF）")


def print_emotion_confirmation(bt, strategy):
    """V6.0: 打印情绪双确认统计"""
    print("\n" + "=" * 80)
    print("V6.0 价格+情绪双确认统计 (Emotion Confirmation)")
    print("=" * 80)

    log = strategy.get_emotion_confirmation_log()
    total_confirmed = bt.emotion_confirmation_count

    print(f"  情绪改善 + 买入行为同时出现: {total_confirmed} 次")
    if log:
        print(f"\n  最近5次双确认事件:")
        for entry in log[-5:]:
            print(f"    {entry['date'].strftime('%Y-%m-%d') if hasattr(entry['date'], 'strftime') else entry['date']} "
                  f"| {entry['behavior']} | {entry['emotion_from']} → {entry['emotion_to']} "
                  f"| 改善幅度: {entry['magnitude']:.2f}")


def print_behavior_contribution(stats):
    """打印行为贡献分析"""
    print("\n" + "=" * 80)
    print("行为贡献分析")
    print("=" * 80)

    for name, s in stats.items():
        if s.get('trades', 0) == 0:
            continue
        tot_c = s['total_contribution'] * 100
        label = "贡献收益" if tot_c >= 0 else "减少收益"
        action = "买入" if name in ['DoubleBottom', 'PanicSell', 'TrendPullback', 'BreakoutConfirm'] else "卖出"
        print(f"  {name:<25} ({action}) {label}: {tot_c:+.2f}%  ({s['count']}次/{s['trades']}笔)")


def print_psychology_distribution(bt):
    """打印情绪状态分布"""
    print("\n" + "=" * 80)
    print("市场情绪状态分布 (EmotionBuilder)")
    print("=" * 80)

    psych_counts = {}
    for s in bt.score_history:
        psych = s.get('psychology', 'Unknown')
        psych_counts[psych] = psych_counts.get(psych, 0) + 1

    total = sum(psych_counts.values())
    if total > 0:
        for state in ['Panic', 'Fear', 'Hope', 'Optimism', 'Euphoria', 'Exhaustion']:
            count = psych_counts.get(state, 0)
            bar = '█' * int(count / total * 40)
            print(f"  {state:<15} {count:>5} ({count/total*100:>5.1f}%)  {bar}")


def run_sensitivity_analysis(df, use_ml=False, emotion_method='weighted'):
    """参数敏感性分析（V6.0 简化版）"""
    import config

    thresholds = [60, 65, 70, 75, 80]
    print(f"\n  --- 确认阈值敏感性（Confirmation Threshold）---")
    print(f"  {'阈值':>8} {'收益率':>10} {'夏普':>8} {'最大回撤':>8} {'交易次数':>8}")
    print(f"  {'-'*48}")

    original = config.CONFIRMATION_THRESHOLD
    for t in thresholds:
        config.CONFIRMATION_THRESHOLD = t
        strategy = V6Strategy(use_ml=use_ml, emotion_method=emotion_method)
        signals = strategy.run(df)
        bt = V6Backtest(df, start_date='2025-09-01')
        results = bt.run(signals)
        print(f"  {t:>8} {results['strategy_return'] * 100:>9.2f}% {results['sharpe_ratio']:>7.3f} {results['max_drawdown'] * 100:>7.2f}% {results['total_trades']:>8}")

    config.CONFIRMATION_THRESHOLD = original


def plot_results(df, signals, bt, results):
    """绘制 V6.0 信号图"""
    if not HAS_MATPLOTLIB:
        return

    df_plot = df[df.index >= '2025-09-01'].copy()
    signal_map = {s['date']: s for s in signals}

    buy_dates, buy_prices, buy_labels = [], [], []
    sell_dates, sell_prices, sell_labels = [], [], []

    for s in signals:
        if s['date'] < pd.Timestamp('2025-09-01'):
            continue
        # V6: confirmed_buy_events 在 score_breakdown 中
        buy_events = s.get('score_breakdown', {}).get('buy', {}).get('events', [])
        sell_events = s.get('score_breakdown', {}).get('sell', {}).get('events', [])
        if buy_events and s['date'] in df_plot.index:
            buy_dates.append(s['date'])
            buy_prices.append(df_plot.loc[s['date'], 'close'])
            buy_labels.append(buy_events[0].get('name', 'Buy'))
        if sell_events and s['date'] in df_plot.index:
            sell_dates.append(s['date'])
            sell_prices.append(df_plot.loc[s['date'], 'close'])
            sell_labels.append(sell_events[0].get('name', 'Sell'))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 10),
                                     gridspec_kw={'height_ratios': [3, 1]}, sharex=True)
    fig.suptitle('A500 ETF (563360) V6.0 Evidence Engine Strategy', fontsize=16, fontweight='bold')

    ax1.plot(df_plot.index, df_plot['close'], color='#333333', linewidth=1.0, label='Close')
    from indicators import calculate_indicators
    df_plot_ind = calculate_indicators(df_plot.copy())
    ax1.plot(df_plot_ind.index, df_plot_ind['MA20'], color='#FF9800', linewidth=1.0, linestyle='--', alpha=0.7, label='MA20')

    for d, p, label in zip(buy_dates, buy_prices, buy_labels):
        ax1.scatter(d, p, color='#00C853', s=100, marker='^', edgecolors='white', linewidths=0.8, zorder=5)
        ax1.annotate(label, (d, p), textcoords="offset points",
                     xytext=(0, 12), ha='center', fontsize=6, color='#00C853',
                     bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.8))

    for d, p, label in zip(sell_dates, sell_prices, sell_labels):
        ax1.scatter(d, p, color='#FF1744', s=100, marker='v', edgecolors='white', linewidths=0.8, zorder=5)
        ax1.annotate(label, (d, p), textcoords="offset points",
                     xytext=(0, -15), ha='center', fontsize=6, color='#FF1744',
                     bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.8))

    ax1.set_ylabel('Price', fontsize=11)
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(True, alpha=0.3)

    colors = ['#4CAF50' if df_plot['close'].iloc[i] >= df_plot['close'].iloc[i-1]
              else '#F44336' for i in range(1, len(df_plot))]
    colors.insert(0, '#4CAF50')
    ax2.bar(df_plot.index, df_plot['volume'], color=colors, alpha=0.6, width=0.8)
    ax2.set_ylabel('Volume', fontsize=11)
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(DateFormatter('%Y-%m'))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=8)

    plt.tight_layout()
    plt.show()
    plt.close()


if __name__ == "__main__":
    evaluate_strategy()
