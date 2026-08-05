"""
V5.0 策略配置文件 —— Behavior Lifecycle + Market Psychology
============================================================

核心升级：
- 行为生命周期（Candidate → Observation → Confirmed → Executed → Finished）
- Event Engine（持久化事件状态）
- Crowd Psychology（市场情绪状态）
- Reward/Risk Evaluation（赔率评估）
- Acceleration/Deceleration（速度跟踪）
- 自动参数网格搜索 + Pareto 最优

设计原则：
- 行为 != 信号
- 情绪 != 拐点
- 候选 != 执行
- 指标用于解释市场，不是触发交易
"""

# ============================================================
# 基础配置
# ============================================================
STOCK_CODE = '563360'
DB_FILENAME = 'stock_data.db'

# ============================================================
# 指标参数
# ============================================================
MA_SHORT = 5
MA_MID = 10
MA_LONG = 20
RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2
VOL_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VWAP_PERIOD = 1

# ============================================================
# 市场状态识别参数
# ============================================================
REGIME_ADX_THRESHOLD = 20
REGIME_MA_SLOPE_MIN = 0.001
REGIME_BB_WIDTH_LOW = 0.02
REGIME_BB_WIDTH_HIGH = 0.06
REGIME_VOLATILITY_LOOKBACK = 20

# ============================================================
# 行为识别参数
# ============================================================

# Behavior 1: Double Bottom（二次探底）
DOUBLE_BOTTOM_LOOKBACK = 30
DOUBLE_BOTTOM_REBOUND_MIN = 0.015
DOUBLE_BOTTOM_SECOND_LOW_MAX = 0.985
DOUBLE_BOTTOM_VOL_SHRINK = 0.75
DOUBLE_BOTTOM_SCORE = 45

# Behavior 2: Momentum Exhaustion（冲高衰竭）
MOMO_EXH_LOOKBACK = 5
MOMO_EXH_RETURN_THRESHOLD = 0.04
MOMO_EXH_RSI_RISE_MIN = 10
MOMO_EXH_VOL_EXPAND = 1.3
MOMO_EXH_ACCEL_DECLINE = 0.7
MOMO_EXH_SCORE = 55

# Behavior 3: Trend Pullback（趋势回踩）
PULLBACK_MA_DIST = 0.02
PULLBACK_VOL_SHRINK = 0.9
PULLBACK_REQUIRE_BULL = False
PULLBACK_SCORE = 30

# Behavior 4: False Break（假突破）
FALSE_BREAK_LOOKBACK = 3
FALSE_BREAK_BREAK_DIST = 0.01
FALSE_BREAK_VOL_RATIO = 1.0
FALSE_BREAK_FALLBACK = 0.003
FALSE_BREAK_SCORE = 30

# Behavior 5: Breakout Confirmation（真突破）
BREAKOUT_CONFIRM_DAYS = 2
BREAKOUT_VOL_INCREASE = 1.2
BREAKOUT_PRICE_RISE = 0.008
BREAKOUT_SCORE = 35

# Behavior 6: Trend Failure（趋势衰退）
TREND_FAIL_MA_SLOPE_NEG = -0.001
TREND_FAIL_MA5_BELOW_MA10 = True
TREND_FAIL_ADX_DECLINE = 2
TREND_FAIL_ATR_EXPAND = 1.3
TREND_FAIL_SCORE = 45

# Behavior 7: Panic Sell（恐慌杀跌）
PANIC_SELL_LOOKBACK = 3
PANIC_SELL_DROP_THRESHOLD = -0.04
PANIC_SELL_ATR_EXPAND = 1.3
PANIC_SELL_VOL_EXPLODE = 1.5
PANIC_SELL_Z_THRESHOLD = -1.8
PANIC_SELL_SCORE = 45

# ============================================================
# 辅助评分因子
# ============================================================
AUX_MA20_TURNING_UP = 12
AUX_ADX_BULL_SUPPORT = 12
AUX_VOLUME_SUPPORT = 12
AUX_RSI_OVERSOLD_REBOUND = 15
AUX_DIVERGENCE_BULL = 18
AUX_RSI_BEAR_DIVERGENCE = 22
AUX_VOLUME_DECLINE = 18
AUX_MA5_MA10_DEAD_CROSS = 12
AUX_ZSCORE_EXTREME = 18

# ============================================================
# V5.0 新增：行为生命周期参数
# ============================================================

# 生命周期状态
LIFECYCLE_STATES = [
    'Candidate',      # 候选：行为首次被检测到
    'Observation',    # 观察：进入观察窗口，跟踪证据变化
    'Confirmed',      # 确认：置信度达到阈值，准备执行
    'Executed',       # 执行：已触发交易
    'Finished',       # 结束：交易完成或事件过期
]

# 观察窗口配置
OBSERVATION_WINDOW_MIN = 2   # 最少观察2个交易日
OBSERVATION_WINDOW_MAX = 5   # 最多观察5个交易日
CONFIRMATION_THRESHOLD = 70  # 置信度 >= 70 才能确认执行
EXPIRY_THRESHOLD = 25        # 置信度 < 25 则过期取消
CONFIDENCE_BASE = 40         # 新候选事件的初始置信度

# 置信度调整参数
CONFIDENCE_INCREMENT = 8     # 每满足一个确认条件，增加8分
CONFIDENCE_DECREMENT = 10    # 每出现一个不利证据，减少10分
CONFIDENCE_MAX = 100         # 置信度上限
CONFIDENCE_MIN = 0           # 置信度下限

# ============================================================
# V5.0 新增：Crowd Psychology 参数
# ============================================================

# 市场情绪状态（按强度递增）
PSYCHOLOGY_STATES = [
    'Panic',       # 恐慌：极端下跌，散户割肉
    'Fear',        # 恐惧：悲观蔓延，观望为主
    'Hope',        # 希望：初见止跌，抄底犹豫
    'Optimism',    # 乐观：趋势确认，积极买入
    'Euphoria',    # 狂热：追涨杀跌，FOMO阶段
    'Exhaustion',  # 衰竭：动能耗尽，见顶信号
]

# 情绪状态切换阈值
PSYCH_Z_PANIC = -2.2        # Z-score < -2.2 → Panic
PSYCH_Z_FEAR = -1.5         # Z-score < -1.5 → Fear
PSYCH_Z_HOPE = -0.5         # Z-score between -1.5 and -0.5 → Hope
PSYCH_Z_OPTIMISM = 0.8      # Z-score between 0.5 and 1.5 → Optimism
PSYCH_Z_EUPHORIA = 1.5      # Z-score > 1.5 + RSI > 70 → Euphoria

# 情绪切换需要确认天数（避免噪声）
PSYCH_SWITCH_CONFIRM_DAYS = 2

# ============================================================
# V5.0 新增：Reward / Risk Evaluation 参数
# ============================================================

# Reward 因子权重
REWARD_WEIGHTS = {
    'dist_from_60d_high': 0.25,    # 距离60日高点越远，上涨空间越大
    'dist_from_60d_low': 0.15,     # 距离60日低点越近，安全边际越高
    'ma20_deviation': 0.20,        # 偏离MA20的程度
    'atr_position': 0.15,          # ATR位置（价格在近期波动范围内的位置）
    'volatility_percentile': 0.10, # 波动率分位数
    'trend_strength': 0.15,        # 趋势强度（ADX）
}

# Risk 因子权重
RISK_WEIGHTS = {
    'drawdown_risk': 0.30,          # 回撤风险（距近期高点距离）
    'volatility_risk': 0.20,         # 波动率风险（当前波动率 vs 历史）
    'trend_reversal_risk': 0.25,     # 趋势反转风险（ADX下降+MA走平）
    'volume_risk': 0.15,             # 成交量异常风险
    'ma_deviation_risk': 0.10,       # 均线偏离风险
}

# Reward/Risk 评分范围
REWARD_SCORE_MAX = 50     # Reward最高50分
RISK_SCORE_MAX = 50       # Risk最高50分

# ============================================================
# V5.0 新增：Acceleration / Deceleration 参数
# ============================================================

# 加速度计算
ACCEL_WINDOW_RECENT = 3    # 近期收益率窗口
ACCEL_WINDOW_PRIOR = 3     # 前期收益率窗口
ACCEL_FOMO_THRESHOLD = 0.02  # 加速度 > 2% → FOMO
ACCEL_DECEL_THRESHOLD = -0.01  # 减速度 < -1% → 动能衰减

# ============================================================
# V5.0 新增：Scoring Engine（新评分公式）
# ============================================================

# Final Buy Score = BehaviorScore + Confidence + Reward - Risk
# 各项权重
SCORE_BEHAVIOR_WEIGHT = 0.40     # 行为分数的权重
SCORE_CONFIDENCE_WEIGHT = 0.25   # 置信度的权重
SCORE_REWARD_WEIGHT = 0.25       # 赔率的权重
SCORE_RISK_WEIGHT = 0.10         # 风险的权重（负向）

# 评分 → 仓位映射
BUY_SCORE_THRESHOLDS = [
    (100, 0.60),  # >= 100: 目标仓位60%
    (85, 0.50),   # >= 85: 目标仓位50%
    (75, 0.40),   # >= 75: 目标仓位40%
    (65, 0.30),   # >= 65: 目标仓位30%
    (55, 0.20),   # >= 55: 目标仓位20%
    (45, 0.12),   # >= 45: 目标仓位12%
    (35, 0.06),   # >= 35: 目标仓位6%
]

# ============================================================
# 市场状态权重系数
# ============================================================
REGIME_WEIGHTS = {
    'Bull': {
        'buy_mult': 1.15,
        'sell_div': 0.80,
    },
    'Bear': {
        'buy_mult': 0.85,
        'sell_div': 1.15,
    },
    'Range': {
        'buy_mult': 1.0,
        'sell_div': 1.0,
    },
    'Unknown': {
        'buy_mult': 0.8,
        'sell_div': 1.0,
    },
}

# ============================================================
# 交易执行参数
# ============================================================
MAX_POSITION = 0.60
MIN_TRADE_VALUE = 50
SLIPPAGE = 0.001
COMMISSION = 0.0003

# ============================================================
# 回测参数
# ============================================================
BACKTEST_START = '2025-09-01'
INITIAL_CASH = 10000.0
INITIAL_POSITION = 0.60

# ============================================================
# V5.0 新增：Grid Search 参数
# ============================================================
GRID_SEARCH_PARAMS = {
    'OBSERVATION_WINDOW_MAX': [3, 4, 5, 6],
    'CONFIRMATION_THRESHOLD': [60, 65, 70, 75, 80],
    'CONFIDENCE_INCREMENT': [5, 8, 10, 12],
    'EXPIRY_THRESHOLD': [15, 20, 25, 30],
    'DOUBLE_BOTTOM_REBOUND_MIN': [0.01, 0.015, 0.02],
    'MOMO_EXH_ACCEL_DECLINE': [0.5, 0.6, 0.7, 0.8],
    'PULLBACK_MA_DIST': [0.015, 0.02, 0.025],
    'PANIC_SELL_DROP_THRESHOLD': [-0.03, -0.04, -0.05],
}

# Pareto 优化目标权重
PARETO_OBJECTIVES = {
    'strategy_return': 1.0,    # 收益率权重
    'sharpe_ratio': 1.0,       # 夏普比率权重
    'max_drawdown': 1.0,       # 最大回撤权重（越小越好）
    'calmar_ratio': 1.0,       # Calmar比率权重
    'win_rate': 0.5,           # 胜率权重
    'total_trades': 0.3,       # 交易次数权重（适中的交易次数更好）
}

# ============================================================
# V5.0 扩展接口
# ============================================================
FUTURE_FACTORS = {
    'news_sentiment': False,
    'etf_flow': False,
    'northbound_flow': False,
    'margin_balance': False,
    'macro_liquidity': False,
    'multi_etf_rotation': False,
    'ml_model': False,
}
