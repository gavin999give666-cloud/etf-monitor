"""
V6.2.3 策略评估模块（稳定化版本）
================================

V6.2.3 新增输出：
- Evidence Explainability 完整分解
- Replay Learning Top10 / Worst10 统计
- ML Confidence 参与统计
- Probability Calibration 评估（Brier/LogLoss）
- Time Decay 衰减曲线
- 每笔交易证据链日志

输出：
1. 回测绩效指标（含 Sharpe/Sortino/Calmar/Kelly）
2. 行为统计
3. 事件统计
4. 评分统计（含 Reward/Risk）
5. Behavior Memory 统计（V6.2.3 增强：含 Psychology 维度）
6. Replay Learning Summary（Top10 / Worst10）
7. Evidence Engine 融合统计（含 Explainability）
8. ML Confidence 参与统计
9. Probability Calibration 评估
10. 情绪双确认统计
11. Time Decay 平均衰减率
12. Replay Engine 验证
13. 参数敏感性分析
14. 可视化
"""
import pandas as pd
import numpy as np
import math
from datetime import datetime

from strategy import V6Strategy
from backtest import V6Backtest
from replay_engine import ReplayEngine
from data_updater import load_data_from_db
import config

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


def evaluate_strategy(use_ml=True, emotion_method='weighted', show_plot=None):
    """V6.2.3 完整策略评估（ML默认启用）"""
    import sys

    # 绘图控制：命令行参数 > 函数参数 > 默认为False（非交互模式不弹窗）
    if show_plot is None:
        show_plot = '--plot' in sys.argv

    print("=" * 80)
    print("A500 ETF V6.2.3 Evidence Engine 稳定化版本 回测评估")
    print("=" * 80)
    print(f"评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"配置: ML={'ON' if use_ml else 'OFF'}, Emotion={emotion_method}")
    print(f"Time Decay: exp(-days/{config.TIME_DECAY_TAU}), min={config.TIME_DECAY_MIN_MULTIPLIER}")
    print(f"Replay Key: (Regime, Behavior, Psychology), Laplace α={config.REPLAY_LAPLACE_ALPHA}")

    df = load_data_from_db()
    if df is None:
        print("无法获取历史数据，评估终止")
        return

    print(f"\n数据范围: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"数据条数: {len(df)}")

    # Step 1: 生成信号
    print("\n[1/7] 运行 V6.2.3 七层策略流水线 (Evidence Engine)...")
    strategy = V6Strategy(use_ml=use_ml, emotion_method=emotion_method)
    signals = strategy.run(df)
    print(f"  信号条数: {len(signals)}")

    # Step 2: 运行回测（集成Replay Learning）
    print("[2/7] 运行增强回测引擎 (Replay Learning)...")
    bt = V6Backtest(df, strategy=strategy)
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

    # Step 6: V6.2.3 Behavior Memory + Replay Summary
    print_behavior_memory(strategy)

    # Step 7: V6 情绪双确认统计
    print_emotion_confirmation(bt, strategy)

    # Step 8: 行为贡献
    print_behavior_contribution(behavior_stats)

    # V6.2.3: ML 参与统计
    print_ml_statistics(bt, strategy)

    # V6.2.3: Calibration 评估
    print_calibration_evaluation(strategy)

    # V6.2.3: Evidence Explainability 摘要
    print_evidence_explainability(bt)

    # V6.2.3: 牛市直入统计
    print_bull_reentry(bt, strategy)

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
        print("\n[V6.2.3 交易促成因子]")
        bt.print_trade_factors(-1)       # 最后一笔
        if len(bt.trades) >= 2:
            bt.print_trade_factors(0)    # 第一笔
        bt.export_all(results=results)    # 每次运行覆盖导出完整回测记录

    # V6.2.3: Time Decay 统计
    print_time_decay_statistics(bt, strategy)

    # Step 10: 绘图（默认关闭，加 --plot 启用）
    if HAS_MATPLOTLIB and show_plot:
        print("\n[图表生成]")
        plot_results(df, signals, bt, results)
    elif HAS_MATPLOTLIB:
        print("\n[图表] 已跳过（使用 --plot 启用绘图）")

    # 打印情绪状态分布
    print_psychology_distribution(bt)

    print("\n" + "=" * 80)
    print("V6.2.3 策略评估完成！")
    print("=" * 80)


def print_performance(results):
    """V6.2.3: 打印绩效指标（增强版）"""
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
    print(f"  {'Sortino比率:':<30} {results.get('sortino_ratio', 0):>8.3f}")
    print(f"  {'Calmar Ratio:':<30} {results['calmar_ratio']:>8.3f}")
    print(f"  {'盈亏比:':<30} {results['profit_factor']:>8.2f}")
    print(f"  {'胜率:':<30} {results['win_rate'] * 100:>8.2f}%")
    print(f"  {'Kelly值:':<30} {results.get('kelly', 0):>8.3f}")
    print(f"  {'期望收益/笔:':<30} {results.get('expectancy', 0):>+8.2f}%")
    print(f"  {'总交易次数:':<30} {results['total_trades']:>8}")
    print(f"  {'盈利交易:':<30} {results['winning_trades']:>8}")
    print(f"  {'亏损交易:':<30} {results['losing_trades']:>8}")
    print(f"  {'平均持仓天数:':<30} {results.get('avg_hold_days', 0):>8.1f}")
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
    print("V6.2.3 评分统计 (含 Reward/Risk)")
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
    """V6.2.3: 打印行为记忆库统计（增强版）"""
    print("\n" + "=" * 80)
    print("V6.2.3 Behavior Memory (Replay Learning) 统计")
    print("=" * 80)

    bm = strategy.get_behavior_memory()
    if bm:
        bm.print_stats()
        # V6.2.3: Replay Summary Top10 / Worst10
        bm.print_replay_summary()
    else:
        print("  行为记忆库未启用或为空")


def print_emotion_confirmation(bt, strategy):
    """V6.2.3: 打印情绪双确认统计"""
    print("\n" + "=" * 80)
    print("V6.2.3 价格+情绪双确认统计 (Emotion Confirmation)")
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


def plot_results(df, signals, bt, results):
    """绘制 V6.2.3 信号图"""
    if not HAS_MATPLOTLIB:
        return

    df_plot = df.copy()

    buy_dates, buy_prices, buy_labels = [], [], []
    sell_dates, sell_prices, sell_labels = [], [], []

    # V6.2.3: 用实际成交记录画买卖标记（而非用信号事件）
    # 确保同一天最多只有一种标记，与回测执行逻辑一致
    for ds in bt.daily_signals:
        if not ds.get('executed'):
            continue
        action = ds.get('action', '')
        date = ds.get('date')
        if date is None or date not in df_plot.index:
            continue
        price = df_plot.loc[date, 'close']
        buy_behaviors = ds.get('buy_behaviors', [])
        sell_behaviors = ds.get('sell_behaviors', [])
        if action == 'BUY':
            buy_dates.append(date)
            buy_prices.append(price)
            buy_labels.append(buy_behaviors[0] if buy_behaviors else 'BUY')
        elif action == 'SELL':
            sell_dates.append(date)
            sell_prices.append(price)
            sell_labels.append(sell_behaviors[0] if sell_behaviors else 'SELL')

    # 空心建议标记：策略"想要"执行的买卖（与回测持仓无关，不受持仓锁定影响）
    # 判定标准：绝对分数 >= config.REC_MARKER_THRESHOLD 且净评分突破 ±SCORE_HOLD_ZONE
    # （与策略目标仓位映射一致；同日买卖双高但净分在HOLD区内的矛盾日不画，每天最多一个标记）
    rec_buy_dates, rec_buy_prices = [], []
    rec_sell_dates, rec_sell_prices = [], []
    for ds in bt.daily_signals:
        date = ds.get('date')
        if date is None or date not in df_plot.index:
            continue
        price = df_plot.loc[date, 'close']
        net = ds.get('buy_score', 0) - ds.get('sell_score', 0)
        if ds.get('buy_score', 0) >= config.REC_MARKER_THRESHOLD and net > config.SCORE_HOLD_ZONE:
            rec_buy_dates.append(date)
            rec_buy_prices.append(price * 0.991)  # 略低于收盘价，避免与实心成交标记重叠
        elif ds.get('sell_score', 0) >= config.REC_MARKER_THRESHOLD and net < -config.SCORE_HOLD_ZONE:
            rec_sell_dates.append(date)
            rec_sell_prices.append(price * 1.009)  # 略高于收盘价

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 10),
                                     gridspec_kw={'height_ratios': [3, 1]}, sharex=True)
    fig.suptitle('A500 ETF (563360) V6.2.3 Evidence Engine Strategy', fontsize=16, fontweight='bold')

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

    # 空心建议标记（买入=空心绿三角，卖出=空心红倒三角）
    if rec_buy_dates:
        ax1.scatter(rec_buy_dates, rec_buy_prices, facecolors='none', edgecolors='#00C853',
                    s=50, marker='^', linewidths=1.2, zorder=4, label='买入建议(空心)')
    if rec_sell_dates:
        ax1.scatter(rec_sell_dates, rec_sell_prices, facecolors='none', edgecolors='#FF1744',
                    s=50, marker='v', linewidths=1.2, zorder=4, label='卖出建议(空心)')

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


# ============================================================
# V6.2.3 新增评估函数
# ============================================================

def print_ml_statistics(bt, strategy):
    """V6.2.3: 打印ML参与统计"""
    print("\n" + "=" * 80)
    print("V6.2.3 ML Confidence 参与统计")
    print("=" * 80)

    ml_summary = strategy.get_ml_summary()
    print(f"  ML状态: {ml_summary.get('status', 'unknown')}")

    if ml_summary.get('status') == 'trained':
        ml = ml_summary.get('ml', {})
        cal = ml_summary.get('calibration', {})
        print(f"  模型类型: {ml.get('model_type', 'N/A')}")
        print(f"  训练准确率: {ml.get('train_accuracy', 'N/A')}")
        print(f"  交叉验证准确率: {ml.get('cv_accuracy', 'N/A')}")
        print(f"  CV Brier: {ml.get('cv_brier_score', 'N/A')}")
        print(f"  总训练样本: {ml_summary.get('total_samples', 0)}")
        print(f"  重训练次数: {ml_summary.get('retrain_count', 0)}")
        print(f"  校准启用: {ml_summary.get('calibration_enabled', False)}")
    else:
        print(f"  样本数: {ml_summary.get('samples', 0)}")

    # 统计ML实际参与次数
    ml_participated = 0
    total_fusions = 0
    for ev_entry in bt.evidence_history:
        for buy_ev in ev_entry.get('buy_evidence', []):
            sources = buy_ev.get('sources', {})
            if sources.get('ml', {}).get('contribution', 0) != 0:
                ml_participated += 1
            total_fusions += 1

    if total_fusions > 0:
        print(f"  ML实际参与: {ml_participated}/{total_fusions} "
              f"({ml_participated/total_fusions*100:.0f}% 的证据融合)")


def print_calibration_evaluation(strategy):
    """V6.2.3: 打印概率校准评估"""
    print("\n" + "=" * 80)
    print("V6.2.3 Probability Calibration 评估")
    print("=" * 80)

    ml_summary = strategy.get_ml_summary()
    cal = ml_summary.get('calibration', {})

    if cal.get('is_fitted'):
        print(f"  校准方法: {cal.get('method', 'N/A')}")
        print(f"  {'':>12} {'校准前':>12} {'校准后':>12} {'变化':>12}")
        if cal.get('brier_before') is not None:
            b_before = cal['brier_before']
            b_after = cal['brier_after']
            print(f"  Brier:  {b_before:>11.4f}  {b_after:>11.4f}  {(b_after-b_before):>+11.4f}")
        if cal.get('logloss_before') is not None:
            l_before = cal['logloss_before']
            l_after = cal['logloss_after']
            print(f"  LogLoss:{l_before:>11.4f}  {l_after:>11.4f}  {(l_after-l_before):>+11.4f}")

        if cal.get('calibration_improved'):
            print(f"  Status: CALIBRATION HELPS ✓")
        else:
            print(f"  Status: CALIBRATION DEGRADED ✗ (已自动禁用)")

        print(f"  校准当前状态: {'启用' if ml_summary.get('calibration_enabled') else '禁用'}")
    else:
        print(f"  校准器未拟合（样本不足）")


def print_evidence_explainability(bt):
    """V6.2.3: 打印证据可解释性摘要"""
    print("\n" + "=" * 80)
    print("V6.2.3 Evidence Explainability 摘要")
    print("=" * 80)

    if not bt.evidence_history:
        print("  无证据历史数据")
        return

    # 统计各证据源的平均贡献
    rule_contribs = []
    replay_contribs = []
    ml_contribs = []
    emotion_contribs = []
    pre_decays = []
    decay_mults = []
    finals = []

    for ev_entry in bt.evidence_history:
        for buy_ev in ev_entry.get('buy_evidence', []):
            sources = buy_ev.get('sources', {})
            rule_contribs.append(sources.get('rule', {}).get('contribution', 0))
            replay_contribs.append(sources.get('replay', {}).get('contribution', 0))
            ml_contribs.append(sources.get('ml', {}).get('contribution', 0))
            emotion_contribs.append(sources.get('emotion', {}).get('contribution', 0))
            pre_decays.append(buy_ev.get('pre_decay', 0))
            decay_mults.append(buy_ev.get('decay_multiplier', 1.0))
            finals.append(buy_ev.get('final', 0))

    n = len(pre_decays)
    if n == 0:
        print("  无证据融合数据（可能无买入事件）")
        return

    # 取非零项计算平均
    def avg_nonzero(arr):
        nonzero = [x for x in arr if x != 0]
        return np.mean(nonzero) if nonzero else 0

    print(f"\n  证据融合次数: {n}")
    print(f"  ┌─ 各证据源平均贡献 ──────────────────────────")
    print(f"  │ Rule:     {avg_nonzero(rule_contribs):+.1f}  (参与率: {sum(1 for x in rule_contribs if x!=0)/n*100:.0f}%)")
    print(f"  │ Replay:   {avg_nonzero(replay_contribs):+.1f}  (参与率: {sum(1 for x in replay_contribs if x!=0)/n*100:.0f}%)")
    print(f"  │ ML:       {avg_nonzero(ml_contribs):+.1f}  (参与率: {sum(1 for x in ml_contribs if x!=0)/n*100:.0f}%)")
    print(f"  │ Emotion:  {avg_nonzero(emotion_contribs):+.1f}  (参与率: {sum(1 for x in emotion_contribs if x!=0)/n*100:.0f}%)")
    print(f"  ├─ 融合统计 ──────────────────────────────────")
    print(f"  │ PreDecay均值:    {np.mean(pre_decays):.1f}")
    print(f"  │ TimeDecay均值:   {np.mean(decay_mults):.3f}")
    print(f"  │ Final均值:       {np.mean(finals):.1f}")
    print(f"  └{'─'*45}")

    # V6.2.3: 显示最近一笔交易的证据分解
    if bt.trades:
        last_trade = bt.trades[-1]
        print(f"\n  最近交易 (#{last_trade.get('trade_id', '?')}) 证据分解:")
        ef = last_trade.get('entry_factors', {})
        for key, val in ef.items():
            if key in ('rule_confidence', 'replay_confidence', 'ml_confidence', 'emotion_bonus'):
                label = val.get('label', key)
                contrib = val.get('contribution', 0)
                print(f"    {label}: {contrib:+.1f}")
            elif key == 'final_confidence':
                print(f"    最终置信度: {val}")


def print_time_decay_statistics(bt, strategy):
    """V6.2.3: 打印Time Decay统计"""
    print("\n" + "=" * 80)
    print("V6.2.3 Time Decay 统计")
    print("=" * 80)

    if not bt.evidence_history:
        print("  无数据")
        return

    decay_mults = []
    for ev_entry in bt.evidence_history:
        for buy_ev in ev_entry.get('buy_evidence', []):
            dm = buy_ev.get('decay_multiplier', 1.0)
            if dm < 1.0:  # 只统计真正产生衰减的
                decay_mults.append(dm)

    if decay_mults:
        avg_decay = np.mean(decay_mults)
        min_decay = min(decay_mults)
        max_decay = max(decay_mults)
        print(f"  衰减次数: {len(decay_mults)}")
        print(f"  平均衰减率: {avg_decay:.3f}")
        print(f"  最严重衰减: {min_decay:.3f}")
        print(f"  最轻衰减: {max_decay:.3f}")
    else:
        print(f"  所有融合均在宽限期内（无衰减）")

    # 打印衰减曲线
    from behavior_memory import TimeDecay
    td = TimeDecay(
        grace_period=config.TIME_DECAY_GRACE_PERIOD,
        tau=config.TIME_DECAY_TAU,
        min_multiplier=config.TIME_DECAY_MIN_MULTIPLIER,
    )
    print(f"\n  Time Decay 曲线 (exp(-days/{td.tau}), min={td.min_multiplier}):")
    for days in [5, 10, 20, 30, 60, 90, 120]:
        m = td.compute_multiplier(days)
        bar = '█' * int(m * 20)
        print(f"    Day {days:>3}: {m:.3f} {bar}")


def print_bull_reentry(bt, strategy):
    """V6.2.3: 打印牛市止盈后重新入场统计"""
    print("\n" + "=" * 80)
    print("V6.2.3 牛市止盈后重新入场 (Bull Re-entry) 统计")
    print("=" * 80)

    reentry_log = strategy.get_reentry_log()
    if not reentry_log:
        print("  无牛市直入事件")
        return

    print(f"  牛市止盈触发次数: {len(reentry_log)}")
    for entry in reentry_log:
        print(f"    {entry['date']} | Buy={entry['buy_score']:.1f} | Sell={entry['sell_score']:.1f} | 窗口至idx={entry['reentry_until_idx']}")

    # 统计直入窗口内发生买入的天数
    reentry_buy_days = 0
    for s in bt.daily_signals:
        if s.get('bull_reentry') and s.get('action') == 'BUY':
            reentry_buy_days += 1
    print(f"  直入窗口内实际买入: {reentry_buy_days} 次")

    # 统计直入窗口内买入的盈亏
    reentry_trades = []
    for t in bt.trades:
        entry_date = t.get('entry_date')
        # 检查该买入是否在直入窗口内
        for entry in reentry_log:
            reentry_date = entry['date']
            if entry_date and reentry_date:
                # 买入日期在卖出后5天内
                try:
                    ed = pd.Timestamp(entry_date)
                    rd = pd.Timestamp(reentry_date)
                    delta = (ed - rd).days
                    if 0 < delta <= config.BULL_REENTRY_WINDOW:
                        reentry_trades.append(t)
                        break
                except:
                    pass

    if reentry_trades:
        reentry_pnls = [t['pnl_pct'] for t in reentry_trades]
        reentry_wins = [p for p in reentry_pnls if p >= 0]
        print(f"  直入窗口内的交易: {len(reentry_trades)} 笔")
        print(f"  胜率: {len(reentry_wins)/len(reentry_trades)*100:.0f}%")
        print(f"  平均盈亏: {np.mean(reentry_pnls):+.2f}%")
    else:
        print(f"  直入窗口内无交易")


if __name__ == "__main__":
    evaluate_strategy()
