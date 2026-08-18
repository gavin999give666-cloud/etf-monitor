"""
V4.0 策略评估模块

输出：
1. 回测绩效指标
2. 行为统计
3. 评分统计
4. 行为贡献分析
5. 参数敏感性分析
"""
import pandas as pd
import numpy as np
import math
from datetime import datetime

from strategy import run_strategy
from backtest import V4Backtest
from data_updater import load_data_from_db

# 尝试导入matplotlib
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


# ============================================================
# 主评估函数
# ============================================================
def evaluate_strategy():
    """V4.0 完整策略评估"""
    print("=" * 80)
    print("A500 ETF V4.0 行为识别 + 概率评分系统 回测评估")
    print("=" * 80)
    print(f"评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    df = load_data_from_db()
    if df is None:
        print("无法获取历史数据，评估终止")
        return

    print(f"\n数据范围: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"数据条数: {len(df)}")

    # Step 1: 生成信号
    print("\n[1/5] 生成V4.0交易信号...")
    signals = run_strategy(df)
    print(f"  信号条数: {len(signals)}")

    # Step 2: 运行回测
    print("[2/5] 运行增强回测引擎...")
    bt = V4Backtest(df, start_date='2025-09-01')
    results = bt.run(signals)

    if not results:
        print("回测失败")
        return

    # Step 3: 输出绩效指标
    print_performance(results)

    # Step 4: 输出行为统计
    behavior_stats = bt.get_behavior_statistics()
    print_behavior_statistics(behavior_stats)

    # Step 5: 输出评分统计
    score_stats = bt.get_score_statistics()
    print_score_statistics(score_stats)

    # Step 6: 输出行为贡献分析
    print_behavior_contribution(behavior_stats)

    # Step 7: 参数敏感性分析
    print("\n[参数敏感性分析]")
    run_sensitivity_analysis(df)

    # Step 8: 绘图
    if HAS_MATPLOTLIB:
        print("\n[图表生成]")
        plot_results(df, signals, bt, results)

    print("\n" + "=" * 80)
    print("V4.0 策略评估完成！")
    print("=" * 80)


# ============================================================
# 打印函数
# ============================================================

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
    print(f"  {'平均盈利:':<30} {results['avg_profit'] * 100:>8.2f}%")
    print(f"  {'平均亏损:':<30} {results['avg_loss'] * 100:>8.2f}%")
    print(f"  {'最大盈利:':<30} {results['max_profit'] * 100:>8.2f}%")
    print(f"  {'最大亏损:':<30} {results['max_loss'] * 100:>8.2f}%")
    print(f"  {'交易频率(/天):':<30} {results['trade_frequency']:>8.4f}")
    print(f"  {'最终资产:':<30} {results['final_equity']:>10.2f}")
    print(f"  {'最大连续盈利:':<30} {results['max_consecutive_wins']:>8}")
    print(f"  {'最大连续亏损:':<30} {results['max_consecutive_losses']:>8}")


def print_behavior_statistics(stats):
    """打印行为统计"""
    print("\n" + "=" * 80)
    print("行为统计 (Behavior Statistics)")
    print("=" * 80)
    print(f"\n  {'行为名称':<25} {'出现次数':>8} {'触发交易':>8} {'平均贡献':>10} {'总贡献':>10}")
    print(f"  {'-'*65}")

    buy_behaviors = ['DoubleBottom', 'PanicSell', 'TrendPullback', 'BreakoutConfirm']
    sell_behaviors = ['MomentumExhaustion', 'TrendFailure', 'FalseBreak']

    print("  --- 买入行为 ---")
    for name in buy_behaviors:
        s = stats.get(name, {})
        avg_c = s.get('avg_contribution', 0) * 100
        tot_c = s.get('total_contribution', 0) * 100
        print(f"  {name:<25} {s.get('count', 0):>8} {s.get('trades', 0):>8} {avg_c:>9.2f}% {tot_c:>9.2f}%")

    print("\n  --- 卖出行为 ---")
    for name in sell_behaviors:
        s = stats.get(name, {})
        avg_c = s.get('avg_contribution', 0) * 100
        tot_c = s.get('total_contribution', 0) * 100
        print(f"  {name:<25} {s.get('count', 0):>8} {s.get('trades', 0):>8} {avg_c:>9.2f}% {tot_c:>9.2f}%")


def print_score_statistics(stats):
    """打印评分统计"""
    print("\n" + "=" * 80)
    print("评分统计 (Score Statistics)")
    print("=" * 80)
    if not stats:
        print("  无评分数据")
        return

    print(f"\n  平均BuyScore:   {stats['avg_buy_score']:>8.1f}")
    print(f"  平均SellScore:  {stats['avg_sell_score']:>8.1f}")
    print(f"  最大BuyScore:   {stats['max_buy_score']:>8.1f}")
    print(f"  最大SellScore:  {stats['max_sell_score']:>8.1f}")

    print("\n  --- BuyScore 分布 ---")
    buy_dist = stats.get('buy_distribution', {})
    for k, v in buy_dist.items():
        print(f"  {k:<10} {v:>8}")

    print("\n  --- SellScore 分布 ---")
    sell_dist = stats.get('sell_distribution', {})
    for k, v in sell_dist.items():
        print(f"  {k:<10} {v:>8}")


def print_behavior_contribution(stats):
    """打印行为贡献分析"""
    print("\n" + "=" * 80)
    print("行为贡献分析 (Behavior Contribution Analysis)")
    print("=" * 80)
    print("\n  说明：正数表示该行为对收益有正向贡献，负数表示负向。")
    print("  卖出行为的贡献体现为「减少回撤/锁住利润」。\n")

    for name, s in stats.items():
        if s.get('trades', 0) == 0:
            continue
        tot_c = s['total_contribution'] * 100
        label = "贡献收益" if tot_c >= 0 else "减少收益"
        action_type = "买入" if name in ['DoubleBottom', 'PanicSell', 'TrendPullback', 'BreakoutConfirm'] else "卖出"
        print(f"  {name:<25} ({action_type}) {label}: {tot_c:+.2f}%  (触发{s['count']}次, 交易{s['trades']}次)")


# ============================================================
# 参数敏感性分析
# ============================================================

def run_sensitivity_analysis(df):
    """
    参数敏感性分析
    
    测试不同 BuyScore / SellScore 阈值组合的效果
    """
    from config import BUY_DELTA_MAP, SELL_DELTA_MAP

    # 待测试的参数组合
    buy_thresholds = [38, 42, 48, 55]
    sell_thresholds = [38, 42, 48, 55]

    print("\n--- BuyScore 最低阈值敏感性 ---")
    print(f"  {'阈值':>8} {'收益率':>10} {'夏普':>8} {'最大回撤':>8} {'交易次数':>8}")
    print(f"  {'-'*48}")

    for threshold in buy_thresholds:
        import config
        original_map = config.BUY_DELTA_MAP[:]
        # 调整阈值：shift all thresholds proportionally
        new_map = []
        base = original_map[0][0] if original_map else 85
        for t, p in original_map:
            new_map.append((max(20, t - (base - threshold)), p))
        config.BUY_DELTA_MAP = new_map

        signals = run_strategy(df)
        bt = V4Backtest(df, start_date='2025-09-01')
        results = bt.run(signals)

        print(f"  {threshold:>8} {results['strategy_return'] * 100:>9.2f}% {results['sharpe_ratio']:>7.3f} {results['max_drawdown'] * 100:>7.2f}% {results['total_trades']:>8}")

        config.BUY_DELTA_MAP = original_map

    print(f"\n--- SellScore 最低阈值敏感性 ---")
    print(f"  {'阈值':>8} {'收益率':>10} {'夏普':>8} {'最大回撤':>8} {'交易次数':>8}")
    print(f"  {'-'*48}")

    for threshold in sell_thresholds:
        import config
        original_map = config.SELL_DELTA_MAP[:]
        base = original_map[0][0] if original_map else 85
        new_map = []
        for t, p in original_map:
            new_map.append((max(20, t - (base - threshold)), p))
        config.SELL_DELTA_MAP = new_map

        signals = run_strategy(df)
        bt = V4Backtest(df, start_date='2025-09-01')
        results = bt.run(signals)
        print(f"  {threshold:>8} {results['strategy_return'] * 100:>9.2f}% {results['sharpe_ratio']:>7.3f} {results['max_drawdown'] * 100:>7.2f}% {results['total_trades']:>8}")

        config.SELL_DELTA_MAP = original_map


# ============================================================
# 可视化
# ============================================================

def plot_results(df, signals, bt, results):
    """绘制V4.0交易信号图"""
    if not HAS_MATPLOTLIB:
        print("  未安装 matplotlib，跳过图表绘制")
        return

    df_plot = df[df.index >= '2025-09-01'].copy()
    signal_map = {s['date']: s for s in signals}

    # 分离买卖信号
    buy_dates, buy_prices = [], []
    sell_dates, sell_prices = [], []
    buy_labels, sell_labels = [], []

    for s in signals:
        if s['date'] < pd.Timestamp('2025-09-01'):
            continue
        buy_behaviors = s.get('buy_behaviors', [])
        sell_behaviors = s.get('sell_behaviors', [])
        if buy_behaviors and s['date'] in df_plot.index:
            buy_dates.append(s['date'])
            buy_prices.append(df_plot.loc[s['date'], 'close'])
            buy_labels.append(','.join(buy_behaviors))
        if sell_behaviors and s['date'] in df_plot.index:
            sell_dates.append(s['date'])
            sell_prices.append(df_plot.loc[s['date'], 'close'])
            sell_labels.append(','.join(sell_behaviors))

    # 创建图表
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 10),
                                     gridspec_kw={'height_ratios': [3, 1]},
                                     sharex=True)
    fig.suptitle('A500 ETF (563360) V4.0 行为识别策略信号图', fontsize=16, fontweight='bold')

    # 上图：价格 + 均线 + 行为信号
    ax1.plot(df_plot.index, df_plot['close'], color='#333333', linewidth=1.0, label='收盘价')
    from indicators import calculate_indicators
    df_plot_ind = calculate_indicators(df_plot.copy())
    ax1.plot(df_plot_ind.index, df_plot_ind['MA20'], color='#FF9800', linewidth=1.0, linestyle='--', alpha=0.7, label='MA20')
    ax1.plot(df_plot_ind.index, df_plot_ind['MA5'], color='#2196F3', linewidth=0.6, linestyle='--', alpha=0.4, label='MA5')

    # 标注买入行为
    for d, p, label in zip(buy_dates, buy_prices, buy_labels):
        ax1.scatter(d, p, color='#00C853', s=100, marker='^', edgecolors='white', linewidths=0.8, zorder=5)
        ax1.annotate(label, (d, p), textcoords="offset points",
                     xytext=(0, 12), ha='center', fontsize=6, color='#00C853',
                     bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.8))

    # 标注卖出行为
    for d, p, label in zip(sell_dates, sell_prices, sell_labels):
        ax1.scatter(d, p, color='#FF1744', s=100, marker='v', edgecolors='white', linewidths=0.8, zorder=5)
        ax1.annotate(label, (d, p), textcoords="offset points",
                     xytext=(0, -15), ha='center', fontsize=6, color='#FF1744',
                     bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.8))

    ax1.set_ylabel('价格 (元)', fontsize=11)
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(True, alpha=0.3)

    # 下图：成交量
    colors = ['#4CAF50' if df_plot['close'].iloc[i] >= df_plot['close'].iloc[i-1]
              else '#F44336' for i in range(1, len(df_plot))]
    colors.insert(0, '#4CAF50')
    ax2.bar(df_plot.index, df_plot['volume'], color=colors, alpha=0.6, width=0.8)
    ax2.set_ylabel('成交量', fontsize=11)
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(DateFormatter('%Y-%m'))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=8)

    plt.tight_layout()
    plt.show()
    plt.close()


if __name__ == "__main__":
    evaluate_strategy()
