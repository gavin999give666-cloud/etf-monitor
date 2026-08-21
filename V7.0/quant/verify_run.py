"""
验证脚本 —— 跑一组参数确保整个流程能正常工作
=============================================
跑通之后再启动 grid_search_v2.py
"""

import sys
import os
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 1. 修改 config（测试参数 = 诊断报告推荐值）
# ============================================================
import config as cfg

cfg.MIN_HOLD_DAYS = 20
cfg.SCORE_HOLD_ZONE = 20
cfg.TRADE_TARGET_DELTA = 0.03
cfg.MAX_POSITION = 0.90
cfg.CONFIRMATION_THRESHOLD = 70
cfg.REGIME_WEIGHTS['Bull']['sell_div'] = 0.20
cfg.REGIME_WEIGHTS['Bull']['buy_mult'] = 1.50

print("配置已修改:")
print(f"  MIN_HOLD_DAYS = {cfg.MIN_HOLD_DAYS}")
print(f"  SCORE_HOLD_ZONE = {cfg.SCORE_HOLD_ZONE}")
print(f"  TRADE_TARGET_DELTA = {cfg.TRADE_TARGET_DELTA}")
print(f"  MAX_POSITION = {cfg.MAX_POSITION}")
print(f"  CONFIRMATION_THRESHOLD = {cfg.CONFIRMATION_THRESHOLD}")
print(f"  Bull.sell_div = {cfg.REGIME_WEIGHTS['Bull']['sell_div']}")
print(f"  Bull.buy_mult = {cfg.REGIME_WEIGHTS['Bull']['buy_mult']}")

# ============================================================
# 2. 加载数据
# ============================================================
print("\n加载数据...")
from data_updater import load_data_from_db
df = load_data_from_db()
print(f"  数据: {len(df)} 行, {len(df.columns)} 列")
print(f"  日期范围: {df.index[0]} ~ {df.index[-1]}")

# ============================================================
# 3. 计算指标
# ============================================================
print("\n计算指标...")
from indicators import calculate_indicators
df = calculate_indicators(df)
print(f"  指标: {len(df.columns)} 列")

# ============================================================
# 4. 运行策略
# ============================================================
print("\n运行策略...")
from strategy import V6Strategy
strategy = V6Strategy()
signals = strategy.run(df)
buy_signals = sum(1 for s in signals if s.get('buy_score', 0) > s.get('sell_score', 0))
sell_signals = sum(1 for s in signals if s.get('sell_score', 0) > s.get('buy_score', 0))
print(f"  交易日: {len(signals)} 天")
print(f"  买入信号日: {buy_signals} 天")
print(f"  卖出信号日: {sell_signals} 天")

# ============================================================
# 5. 运行回测
# ============================================================
print("\n运行回测...")
from backtest import V6Backtest
bt = V6Backtest(df, strategy=strategy)
bt.run(signals)
results = bt._compute_results()

# ============================================================
# 6. 打印结果
# ============================================================
print("\n" + "=" * 60)
print("  回测结果（优化参数后）")
print("=" * 60)
metrics = [
    ('strategy_return',  '策略收益率',   '%', 100),
    ('excess_return',    '超额收益',     '%', 100),
    ('sharpe_ratio',     '夏普比率',     '',   1),
    ('max_drawdown',     '最大回撤',     '%', -100),
    ('calmar_ratio',     'Calmar比率',   '',   1),
    ('win_rate',         '胜率',         '%', 100),
    ('total_trades',     '交易次数',     '',   1),
    ('avg_hold_days',    '平均持仓天',   '',   1),
    ('profit_factor',    '盈亏比',       '',   1),
    ('expectancy',       '期望值',       '%', 100),
]
for key, label, unit, mult in metrics:
    val = results.get(key, 0)
    print(f"  {label:<12}: {val * mult:+.2f}{unit}")

print(f"\n  基准收益率: {(df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100:.2f}%")
print(f"\n验证完成！可以运行 grid_search_v2.py 开始完整搜索。")
